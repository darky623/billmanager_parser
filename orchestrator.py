"""Main parsing orchestrator.

Per provider:
1. Fetch raw pricelist from BILLmanager
2. Parse → list[ParsedPlan]
3. For each plan: compare snapshot, skip if unchanged
4. For changed/new plans: fetch OS list, map features (fast → LLM fallback), map prices
5. Ensure OS families and OS records exist in cloudsell API
6. POST /v1/pricing-plans/sync with the full active_external_ids + changed plans payload
7. Save snapshots for all processed plans
"""

import structlog
from openai import OpenAI

from api_client.cloudsell import CloudsellApiError, CloudsellClient
from config import ProviderCredentials, settings
from mapper.features import get_location_fields, map_features
from mapper.llm import build_gemini_client
from mapper.prices import map_prices
from parser.client import BillManagerClient
from parser.models import ParsedOS, ParsedPlan
from parser.os_list import parse_os_list
from parser.plans import parse_pricelist
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
            # Find provider record in cloudsell DB
            provider_id = await self._resolve_provider_id(api, creds.base_url)
            if not provider_id:
                log.error("Provider not found in cloudsell DB, skipping", base_url=creds.base_url)
                return

            async with BillManagerClient(
                base_url=creds.base_url,
                username=creds.username,
                password=creds.password.get_secret_value(),
                timeout=settings.provider_timeout,
            ) as bm:
                await self._process_provider(bm, api, provider_id, creds)

        structlog.contextvars.unbind_contextvars("provider")

    async def _process_provider(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        creds: ProviderCredentials,
    ) -> None:
        # 1. Fetch pricelist
        try:
            raw_pricelist = await bm.fetch_pricelist()
        except Exception as exc:
            log.error("Failed to fetch pricelist", error=str(exc))
            return

        # 2. Parse pricelist
        plans = parse_pricelist(raw_pricelist)
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

        for plan in changed_plans:
            log.info("Processing changed plan", plan_id=plan.external_id, name=plan.name)

            payload = await self._build_plan_payload(bm, api, provider_id, creds.base_url, plan)
            if payload:
                plan_payloads.append(payload)

        if not plan_payloads:
            log.warning("No valid plan payloads built despite changes")
            return

        # 5. Sync with cloudsell API
        try:
            result = await api.sync_plans(
                provider_id=provider_id,
                active_external_ids=active_external_ids,
                plans=plan_payloads,
            )
            log.info(
                "Sync complete",
                created=result.get("created", 0),
                deactivated=result.get("deactivated", 0),
            )
        except CloudsellApiError as exc:
            log.error("Failed to sync plans with API", error=str(exc))
            return

        # 6. Save snapshots for changed plans (only after successful sync)
        for plan in changed_plans:
            snap_dict = plan_to_snapshot_dict(
                plan_id=plan.external_id,
                prices=[{"period": p.period, "cost": str(p.cost), "currency": p.currency} for p in plan.prices],
                detail=plan.detail,
            )
            self._snapshot.save(creds.base_url, plan.external_id, snap_dict)

        log.info("Snapshots updated", count=len(changed_plans))

    async def _build_plan_payload(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        provider_host: str,
        plan: ParsedPlan,
    ) -> dict | None:
        plan_ctx = structlog.contextvars.bind_contextvars(plan_id=plan.external_id)

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

        # 4b. Ensure OS families and collect os UUIDs
        os_uuids = await self._ensure_os_records(api, provider_id, os_list, plan.external_id)
        if not os_uuids:
            log.warning("No valid OS UUIDs, skipping plan")
            return None

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

        payload = {
            "name": plan.name,
            "provider_id": provider_id,
            "description": plan.name,
            "server_type": "virtual",
            "external_id": plan.external_id,
            "is_active": True,
            "prices": prices,
            "features": {
                "processor_name": features.processor_name,
                "cores": features.cores,
                "core_frequency": str(features.core_frequency) if features.core_frequency else None,
                "ram": str(features.ram),
                "ram_type": features.ram_type,
                "disk": str(features.disk),
                "disk_type": features.disk_type,
                "network_speed": str(features.network_speed),
                "network_limit": str(features.network_limit),
                "location": location_code,
                "location_raw": location_raw,
                "datacenter_id": plan.datacenter_id,
            },
            "os_list": [str(uid) for uid in os_uuids],
        }

        log.info(
            "Plan payload built",
            plan_id=plan.external_id,
            cores=features.cores,
            ram=str(features.ram),
            disk=str(features.disk),
            prices_count=len(prices),
            os_count=len(os_uuids),
        )

        structlog.contextvars.unbind_contextvars("plan_id")
        return payload

    async def _ensure_os_records(
        self,
        api: CloudsellClient,
        provider_id: str,
        os_list: list[ParsedOS],
        plan_id: int,
    ) -> list:
        os_uuids = []
        families_ensured: set[str] = set()

        for os_entry in os_list:
            # Ensure family exists
            if os_entry.family not in families_ensured:
                try:
                    await api.ensure_os_family(os_entry.family)
                    families_ensured.add(os_entry.family)
                except CloudsellApiError as exc:
                    log.warning("Failed to ensure OS family", family=os_entry.family, error=str(exc))

            # Ensure OS record exists
            try:
                os_uuid = await api.ensure_os(
                    provider_id=provider_id,
                    family=os_entry.family,
                    name=os_entry.display_name,
                    external_id=os_entry.external_id,
                )
                if os_uuid:
                    os_uuids.append(os_uuid)
            except CloudsellApiError as exc:
                log.warning(
                    "Failed to ensure OS record",
                    os_name=os_entry.display_name,
                    plan_id=plan_id,
                    error=str(exc),
                )

        return os_uuids

    async def _resolve_provider_id(self, api: CloudsellClient, base_url: str) -> str | None:
        """Find provider UUID in cloudsell by matching base_url."""
        try:
            providers = await api.get_providers()
        except CloudsellApiError as exc:
            log.error("Failed to fetch providers from API", error=str(exc))
            return None

        for p in providers:
            if p.get("base_url", "").rstrip("/") == base_url.rstrip("/"):
                return p["id"]

        return None
