"""Main parsing orchestrator.

Per provider:
1. Fetch raw pricelist from BILLmanager
2. Parse → list[ParsedPlan]
3. For each plan: compare snapshot, skip if unchanged
4. For changed/new plans: fetch OS list, map features, map prices
5. POST /v1/pricing-plans/sync — includes OS definitions, API handles upsert internally
6. Save snapshots for successfully synced plans
"""

import structlog

from api_client.cloudsell import CloudsellApiError, CloudsellClient
from config import ProviderCredentials, settings
from mapper.features import get_location_fields, map_features
from mapper.prices import map_prices
from parser.client import BillManagerClient
from parser.models import ParsedPlan
from parser.os_list import parse_os_list
from parser.plans import (
    parse_datacenter_ids,
    parse_order_configuration_cost,
    parse_order_default_addons,
    parse_order_setup_fee,
    parse_pricelist,
)
from snapshot.store import SnapshotStore, plan_to_snapshot_dict

log = structlog.get_logger(__name__)


class ParserOrchestrator:
    def __init__(self) -> None:
        self._snapshot = SnapshotStore(base_dir=settings.snapshot_dir)

    async def run_all_providers(self) -> None:
        log.info("Parser run started", provider_count=len(settings.providers))
        for creds in settings.providers:
            await self._run_provider(creds)
        log.info("Parser run completed")

    async def _run_provider(self, creds: ProviderCredentials) -> None:
        structlog.contextvars.bind_contextvars(provider=creds.base_url)
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
                timeout=creds.timeout or settings.provider_timeout,
            ) as bm:
                for server_type in creds.enabled_server_types:
                    await self._process_provider(bm, api, creds.provider_id, creds, server_type)

        structlog.contextvars.unbind_contextvars("provider")

    async def _process_provider(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        creds: ProviderCredentials,
        server_type: str = "virtual",
    ) -> None:
        snapshot_host = creds.base_url if server_type == "virtual" else f"{creds.base_url}/{server_type}"
        # 1. Fetch pricelist for each datacenter
        try:
            raw_default = await bm.fetch_pricelist(server_type=server_type)
            datacenter_ids = parse_datacenter_ids(raw_default)
            if not datacenter_ids:
                datacenter_ids = [None]  # type: ignore[list-item]
            log.info("Datacenters found", count=len(datacenter_ids), ids=datacenter_ids)
        except Exception as exc:
            log.error("Failed to fetch pricelist", error=str(exc))
            return

        # 2. Parse plans across all datacenters
        plans: list[ParsedPlan] = []
        failed_datacenters: list[int | None] = []
        for dc_id in datacenter_ids:
            try:
                raw = await bm.fetch_pricelist(datacenter_id=dc_id, server_type=server_type)
                dc_plans = parse_pricelist(raw, server_type=server_type)
                plans.extend(dc_plans)
            except Exception as exc:
                log.error("Failed to fetch pricelist for datacenter", datacenter_id=dc_id, error=str(exc))
                failed_datacenters.append(dc_id)

        if failed_datacenters:
            log.warning("Datacenters unavailable; skipping deactivation", datacenter_ids=failed_datacenters)

        if not plans:
            log.warning("No plans parsed; keeping existing catalog")
            return

        if server_type != "virtual":
            configurable = [p.external_id for p in plans if "конфигурируем" in p.name.lower()]
            if configurable:
                log.info("Skipping plans without a fixed hardware configuration", ids=configurable)
                plans = [p for p in plans if p.external_id not in configurable]

        if server_type != "virtual":
            complete_plans = []
            for plan in plans:
                try:
                    raw_param = await bm.fetch_os_list(
                        plan.external_id, plan.datacenter_id,
                        plan.prices[0].period, server_type,
                    )
                    setup_fee = parse_order_setup_fee(raw_param)
                    if setup_fee is None:
                        raise ValueError("Order response has no recognizable price summary")
                    configuration_cost = parse_order_configuration_cost(raw_param)
                    if configuration_cost is None or configuration_cost != 0:
                        raise ValueError("Default configuration has an unpriced recurring charge")
                    plan.raw_order_param = raw_param
                    plan.default_addons = parse_order_default_addons(raw_param)
                    for price in plan.prices:
                        price.setup_cost = setup_fee
                    complete_plans.append(plan)
                except Exception as exc:
                    log.warning("Cannot verify physical plan order", plan_id=plan.external_id, error=str(exc))
                    failed_datacenters.append(plan.datacenter_id)
            plans = complete_plans
            if not plans:
                return

        log.info("Plans found in pricelist", count=len(plans))
        active_external_ids = [p.external_id for p in plans]

        # 3. Determine which plans changed
        changed_plans: list[ParsedPlan] = []
        for plan in plans:
            snap_dict = plan_to_snapshot_dict(
                plan_id=plan.external_id,
                prices=[
                    {
                        "period": p.period, "cost": str(p.cost), "currency": p.currency,
                        **({"setup_cost": str(p.setup_cost)} if plan.server_type != "virtual" else {}),
                    }
                    for p in plan.prices
                ],
                detail={**plan.detail, "_default_addons": plan.default_addons},
            )
            if settings.force_full_sync or self._snapshot.has_changed(snapshot_host, plan.external_id, snap_dict):
                changed_plans.append(plan)

        log.info(
            "Plans change summary",
            total=len(plans),
            changed=len(changed_plans),
            unchanged=len(plans) - len(changed_plans),
        )

        if not changed_plans:
            log.info("All plans unchanged, skipping sync")
            if not failed_datacenters:
                try:
                    await api.sync_plans(provider_id, active_external_ids, [], server_type=server_type, finalize=True)
                except CloudsellApiError as exc:
                    log.error("Failed to finalize catalog", server_type=server_type, error=str(exc))
                else:
                    for removed_id in self._snapshot.list_saved_ids(snapshot_host) - set(active_external_ids):
                        self._snapshot.remove(snapshot_host, removed_id)
            return

        # 4. Build plan payloads for changed plans
        plan_payloads: list[dict] = []
        successfully_built: list[ParsedPlan] = []

        for plan in changed_plans:
            log.info("Processing changed plan", plan_id=plan.external_id, name=plan.name)

            payload = await self._build_plan_payload(
                bm, api, provider_id, creds.base_url, plan,
                creds.factor, creds.name_prefix, creds.default_network_speed,
            )
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
        synced_plans: list[ParsedPlan] = []

        batch_starts = list(range(0, len(plan_payloads), _BATCH_SIZE))
        for batch_start in batch_starts:
            batch_payloads = plan_payloads[batch_start : batch_start + _BATCH_SIZE]
            batch_plans = successfully_built[batch_start : batch_start + _BATCH_SIZE]
            batch_num = batch_start // _BATCH_SIZE + 1
            total_batches = len(batch_starts)
            log.info("Syncing batch", batch=f"{batch_num}/{total_batches}", size=len(batch_payloads))
            try:
                result = await api.sync_plans(
                    provider_id=provider_id,
                    active_external_ids=active_external_ids,
                    plans=batch_payloads,
                    server_type=server_type,
                    finalize=False,
                )
                total_created += result.get("created", 0)
                total_deactivated += result.get("deactivated", 0)
                synced_plans.extend(batch_plans)
            except CloudsellApiError as exc:
                log.error("Failed to sync batch", batch=f"{batch_num}/{total_batches}", error=str(exc))
                continue

        log.info("Sync complete", created=total_created, deactivated=total_deactivated)

        # 6. Save snapshots only for plans from successfully synced batches
        for plan in synced_plans:
            snap_dict = plan_to_snapshot_dict(
                plan_id=plan.external_id,
                prices=[
                    {
                        "period": p.period, "cost": str(p.cost), "currency": p.currency,
                        **({"setup_cost": str(p.setup_cost)} if plan.server_type != "virtual" else {}),
                    }
                    for p in plan.prices
                ],
                detail={**plan.detail, "_default_addons": plan.default_addons},
            )
            self._snapshot.save(snapshot_host, plan.external_id, snap_dict)

        if not failed_datacenters and len(synced_plans) == len(changed_plans):
            try:
                await api.sync_plans(provider_id, active_external_ids, [], server_type=server_type, finalize=True)
            except CloudsellApiError as exc:
                log.error("Failed to finalize catalog", server_type=server_type, error=str(exc))
            else:
                for removed_id in self._snapshot.list_saved_ids(snapshot_host) - set(active_external_ids):
                    self._snapshot.remove(snapshot_host, removed_id)

        log.info("Snapshots updated", count=len(synced_plans))

    async def _build_plan_payload(
        self,
        bm: BillManagerClient,
        api: CloudsellClient,
        provider_id: str,
        provider_host: str,
        plan: ParsedPlan,
        factor: float,
        name_prefix: str,
        default_network_speed: float = 0.0,
    ) -> dict | None:
        structlog.contextvars.bind_contextvars(plan_id=plan.external_id)
        try:
            return await self._build_plan_payload_inner(
                bm, api, provider_id, provider_host, plan, factor, name_prefix, default_network_speed,
            )
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
        default_network_speed: float = 0.0,
    ) -> dict | None:
        # 4a. Fetch OS list
        try:
            raw_os = plan.raw_order_param or await bm.fetch_os_list(
                plan_external_id=plan.external_id,
                datacenter_id=plan.datacenter_id,
                period=plan.prices[0].period if plan.prices else 1,
                server_type=plan.server_type,
            )
            os_list = parse_os_list(raw_os, plan.external_id)
        except Exception as exc:
            log.error("Failed to fetch OS list", error=str(exc))
            return None

        if not os_list:
            if plan.server_type == "virtual":
                log.warning("No OS entries for virtual plan; cannot create an order")
                return None
            # Some dedicated providers (YaColo) provision the OS themselves.
            # Keep an internal placeholder so CloudSell's item/OS relation remains valid.
            os_definitions = [{
                "family": "Other",
                "name": "ОС провайдера (выбор недоступен)",
                "external_id": "",
            }]
        else:
            os_definitions = [
                {
                    "family": os_entry.family,
                    "name": os_entry.display_name,
                    "external_id": os_entry.external_id,
                }
                for os_entry in os_list
            ]

        # 4b. Build OS definitions (API handles upsert internally)

        # 4c. Map prices
        prices = map_prices(plan.prices, server_type=plan.server_type)
        if not prices:
            log.warning("No valid prices for plan, skipping")
            return None

        # 4d. Map features
        features = map_features(plan)
        if not features:
            log.error("Failed to extract features for plan, skipping")
            return None

        # Apply provider-level default speed when provider doesn't publish port speed
        if features.network_speed == 0 and default_network_speed > 0:
            from decimal import Decimal as _D
            features = features.model_copy(update={"network_speed": _D(str(default_network_speed))})
            log.debug("Applied default network speed", speed_mbps=default_network_speed)

        location_code, location_raw = get_location_fields(plan)

        type_marker = {"virtual": "", "dedicated": "-D", "auction": "-A"}[plan.server_type]
        plan_name = f"{name_prefix}{type_marker}-{plan.external_id}"
        payload = {
            "name": plan_name,
            "provider_id": provider_id,
            "description": plan_name,
            "server_type": plan.server_type,
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
            "additional_services": {"default_addons": plan.default_addons} if plan.default_addons else None,
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
