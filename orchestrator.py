"""Main parsing orchestrator.

Per provider:
1. Fetch raw pricelist from BILLmanager
2. Parse → list[ParsedPlan]
3. For each plan: compare snapshot, skip if unchanged
4. For changed/new plans: fetch OS list, map features (fast → LLM fallback), map prices
5. POST /v1/pricing-plans/sync — includes OS definitions, API handles upsert internally
6. Save snapshots for successfully synced plans
"""

import structlog
from openai import OpenAI

from api_client.cloudsell import CloudsellApiError, CloudsellClient
from config import ProviderCredentials, settings
from mapper.features import get_location_fields, map_features
from mapper.llm import build_gemini_client
from mapper.prices import map_prices
from parser.client import BillManagerClient
from parser.models import ParsedPlan
from parser.os_list import parse_os_list
from parser.plans import parse_datacenter_ids, parse_pricelist
from snapshot.store import SnapshotStore, plan_to_snapshot_dict

log = structlog.get_logger(__name__)


class ParserOrchestrator:
    def __init__(self) -> None:
        self._snapshot = SnapshotStore(base_dir=settings.snapshot_dir)
        self._llm: OpenAI = build_gemini_client(settings.gemini_api_key.get_secret_value())

    async def run_all_providers(self) -> None:
        log.info("Parser run started", provider_count=len(settings.providers))
        for creds in settings.providers:
            await self._run_provider(creds)
        log.info("Parser run completed")

    async def _run_provider(self, creds: ProviderCredentials) -> None:
        ctx = structlog.contextvars.bind_contextvars(provider=creds.base_url)
        log.info("Processing provider")

        async with CloudsellClient(
            base_url=settings.cloudsell_api_url,
            service_key=settings.cloudsell_service_key.get_secret_value(),
            timeout=settings.api_timeout,
        ) as api:
            async with BillManagerClient(
                base_url=creds.base_url,
                username=creds.username,
                password=creds.password.get_secret_value(),
                timeout=settings.provider_timeout,
            ) as bm:
                await self._process_provider(bm, api, creds.provider_id, creds)

        structlog.contextvars.unbind_contextvars("provider")

    async def _process_provider(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        creds: ProviderCredentials,
    ) -> None:
        # 1. Fetch pricelist for each datacenter
        try:
            raw_default = await bm.fetch_pricelist()
            datacenter_ids = parse_datacenter_ids(raw_default)
            if not datacenter_ids:
                datacenter_ids = [None]  # type: ignore[list-item]
            log.info("Datacenters found", count=len(datacenter_ids), ids=datacenter_ids)
        except Exception as exc:
            log.error("Failed to fetch pricelist", error=str(exc))
            return

        # 2. Parse plans across all datacenters
        plans: list[ParsedPlan] = []
        for dc_id in datacenter_ids:
            try:
                raw = await bm.fetch_pricelist(datacenter_id=dc_id)
                dc_plans = parse_pricelist(raw)
                plans.extend(dc_plans)
            except Exception as exc:
                log.error("Failed to fetch pricelist for datacenter", datacenter_id=dc_id, error=str(exc))

        if not plans:
            log.warning("No plans parsed from pricelist")
            return

        log.info("Plans found in pricelist", count=len(plans))
        active_external_ids = [p.external_id for p in plans]

        # 3. Determine which plans changed
        changed_plans: list[ParsedPlan] = []
        for plan in plans:
            snap_dict = plan_to_snapshot_dict(
                plan_id=plan.external_id,
                prices=[{"period": p.period, "cost": str(p.cost), "currency": p.currency} for p in plan.prices],
                detail=plan.detail,
            )
            if self._snapshot.has_changed(creds.base_url, plan.external_id, snap_dict):
                changed_plans.append(plan)

        log.info(
            "Plans change summary",
            total=len(plans),
            changed=len(changed_plans),
            unchanged=len(plans) - len(changed_plans),
        )

        if not changed_plans:
            log.info("All plans unchanged, skipping sync")
            return

        # 4. Build plan payloads for changed plans
        plan_payloads: list[dict] = []
        successfully_built: list[ParsedPlan] = []

        for plan in changed_plans:
            log.info("Processing changed plan", plan_id=plan.external_id, name=plan.name)

            payload = await self._build_plan_payload(bm, api, provider_id, creds.base_url, plan, creds.factor, creds.name_prefix)
            if payload:
                plan_payloads.append(payload)
                successfully_built.append(plan)

        if not plan_payloads:
            log.warning("No valid plan payloads built despite changes")
            return

        # 5. Sync with cloudsell API in batches to avoid timeouts on large providers
        _BATCH_SIZE = 50
        total_created = 0
        total_deactivated = 0

        for batch_start in range(0, len(plan_payloads), _BATCH_SIZE):
            batch = plan_payloads[batch_start : batch_start + _BATCH_SIZE]
            batch_num = batch_start // _BATCH_SIZE + 1
            total_batches = (len(plan_payloads) + _BATCH_SIZE - 1) // _BATCH_SIZE
            log.info("Syncing batch", batch=f"{batch_num}/{total_batches}", size=len(batch))
            try:
                result = await api.sync_plans(
                    provider_id=provider_id,
                    active_external_ids=active_external_ids,
                    plans=batch,
                )
                total_created += result.get("created", 0)
                total_deactivated += result.get("deactivated", 0)
            except CloudsellApiError as exc:
                log.error("Failed to sync batch", batch=f"{batch_num}/{total_batches}", error=str(exc))
                return

        log.info("Sync complete", created=total_created, deactivated=total_deactivated)

        # 6. Save snapshots only for plans that were successfully built and synced
        for plan in successfully_built:
            snap_dict = plan_to_snapshot_dict(
                plan_id=plan.external_id,
                prices=[{"period": p.period, "cost": str(p.cost), "currency": p.currency} for p in plan.prices],
                detail=plan.detail,
            )
            self._snapshot.save(creds.base_url, plan.external_id, snap_dict)

        log.info("Snapshots updated", count=len(successfully_built))

    async def _build_plan_payload(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        provider_host: str,
        plan: ParsedPlan,
        factor: float,
        name_prefix: str,
    ) -> dict | None:
        structlog.contextvars.bind_contextvars(plan_id=plan.external_id)
        try:
            return await self._build_plan_payload_inner(bm, api, provider_id, provider_host, plan, factor, name_prefix)
        finally:
            structlog.contextvars.unbind_contextvars("plan_id")

    async def _build_plan_payload_inner(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        provider_host: str,
        plan: ParsedPlan,
        factor: float,
        name_prefix: str,
    ) -> dict | None:
        # 4a. Fetch OS list
        try:
            raw_os = await bm.fetch_os_list(
                plan_external_id=plan.external_id,
                datacenter_id=plan.datacenter_id,
                period=plan.prices[0].period if plan.prices else 1,
            )
            os_list = parse_os_list(raw_os, plan.external_id)
        except Exception as exc:
            log.error("Failed to fetch OS list", error=str(exc))
            return None

        if not os_list:
            log.warning("No OS entries for plan, skipping")
            return None

        # 4b. Build OS definitions (API handles upsert internally)
        os_definitions = [
            {
                "family": os_entry.family,
                "name": os_entry.display_name,
                "external_id": os_entry.external_id,
            }
            for os_entry in os_list
        ]

        # 4c. Map prices
        prices = map_prices(plan.prices)
        if not prices:
            log.warning("No valid prices for plan, skipping")
            return None

        # 4d. Map features (fast path → LLM fallback)
        features = map_features(plan, self._llm, settings.gemini_model)
        if not features:
            log.error("Failed to extract features for plan, skipping")
            return None

        location_code, location_raw = get_location_fields(plan)

        plan_name = f"{name_prefix}-{plan.external_id}"
        payload = {
            "name": plan_name,
            "provider_id": provider_id,
            "description": plan_name,
            "server_type": "virtual",
            "external_id": plan.external_id,
            "is_active": True,
            "factor": str(factor),
            "prices": prices,
            "features": {
                "processor_name": (features.processor_name or "")[:50],
                "cores": features.cores,
                "core_frequency": str(features.core_frequency) if features.core_frequency else None,
                "ram": str(features.ram * 1024),
                "ram_type": features.ram_type,
                "disk": str(features.disk * 1024),
                "disk_type": features.disk_type,
                "network_speed": str(features.network_speed),
                "network_limit": str(features.network_limit),
                "location": location_code,
                "location_raw": location_raw[:40] if location_raw else None,
                "datacenter_id": plan.datacenter_id,
            },
            "os_definitions": os_definitions,
        }

        log.info(
            "Plan payload built",
            plan_id=plan.external_id,
            cores=features.cores,
            ram=str(features.ram),
            disk=str(features.disk),
            prices_count=len(prices),
            os_count=len(os_definitions),
        )

        return payload

