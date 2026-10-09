"""Parse pricelist response into ParsedPlan objects."""

import json
import re
from decimal import Decimal, InvalidOperation

import structlog

from parser.models import ParsedPlan, ParsedPrice, RawPlanElem

log = structlog.get_logger(__name__)

_CURRENCY_MAP = {
    "₽": "rub",
    "р.": "rub",
    "p.": "rub",
    "rub": "rub",
    "RUB": "rub",
    "€": "eur",
    "eur": "eur",
    "EUR": "eur",
    "$": "usd",
    "usd": "usd",
    "USD": "usd",
}


def parse_datacenter_ids(raw_bytes: bytes) -> list[int]:
    """Extract all datacenter IDs from pricelist slist."""
    data = json.loads(raw_bytes)
    slists = data.get("doc", {}).get("slist", [])
    dc_node = next((x for x in slists if isinstance(x, dict) and x.get("$name") == "datacenter"), None)
    if not dc_node:
        return []
    ids = []
    for val in dc_node.get("val", []):
        try:
            ids.append(int(val["$key"]))
        except (KeyError, ValueError):
            continue
    return ids


def parse_pricelist(raw_bytes: bytes, server_type: str = "virtual") -> list[ParsedPlan]:
    """Parse raw BILLmanager pricelist JSON into a list of ParsedPlan."""
    data = json.loads(raw_bytes)
    doc = data.get("doc", {})

    list_nodes = doc.get("list", [])
    if not list_nodes:
        log.warning("pricelist: 'list' is empty")
        return []

    # BILLmanager uses a separate node name for each physical product type.
    vds_node = next(
        (n for n in list_nodes if n.get("$name") in ("vds", "dedic", "not-install", "pricelist")),
        list_nodes[0],
    )
    elems = vds_node.get("elem", [])
    if not elems:
        log.warning("pricelist: no 'elem' found in list node")
        return []

    plans: list[ParsedPlan] = []
    for elem_raw in elems:
        plan = _parse_elem(elem_raw, raw_bytes, server_type)
        if plan:
            plans.append(plan)

    log.info("Parsed plans from pricelist", count=len(plans))
    return plans


def _parse_elem(elem_raw: dict, raw_bytes: bytes, server_type: str = "virtual") -> ParsedPlan | None:
    try:
        elem = RawPlanElem.model_validate(elem_raw)
    except Exception as exc:
        log.warning("Failed to validate plan elem", error=str(exc), elem=str(elem_raw)[:200])
        return None

    external_id_str = elem.id.value
    if not external_id_str:
        return None

    try:
        external_id = int(external_id_str)
    except ValueError:
        log.warning("Invalid external_id", value=external_id_str)
        return None

    prices = _parse_prices(elem.prices.price, external_id)
    if not prices or _all_zero(prices):
        log.debug("Skipping plan: no valid prices", plan_id=external_id)
        return None

    try:
        dc_id = int(elem.datacenter.id.value)
    except ValueError:
        log.warning("Invalid datacenter id", plan_id=external_id, value=elem.datacenter.id.value)
        return None

    return ParsedPlan(
        external_id=external_id,
        server_type=server_type,
        name=elem.title.value,
        description_raw=elem.description.value,
        detail=elem.detail_as_dict(),
        datacenter_id=dc_id,
        datacenter_name=elem.datacenter.value.value,
        prices=prices,
        raw_json=raw_bytes,
    )


def _parse_prices(price_nodes: list, external_id: int) -> list[ParsedPrice]:
    result: list[ParsedPrice] = []
    for entry in price_nodes:
        try:
            period = int(entry.period.value)
            cost = Decimal(entry.cost.value)
            currency = _CURRENCY_MAP.get(entry.currency.value, entry.currency.value.lower())
            setup_value = next(
                (value for value in (entry.setup, entry.setup_cost, entry.installation) if value is not None),
                None,
            )
            if isinstance(setup_value, dict):
                setup_value = setup_value.get("$")
            setup_cost = Decimal(str(setup_value)) if setup_value is not None else None
            result.append(ParsedPrice(period=period, cost=cost, currency=currency, setup_cost=setup_cost))
        except (ValueError, InvalidOperation) as exc:
            log.warning("Failed to parse price entry", plan_id=external_id, error=str(exc))
    return result


def _all_zero(prices: list[ParsedPrice]) -> bool:
    return all(p.cost == Decimal("0.00") for p in prices)


def parse_order_setup_fee(raw_bytes: bytes) -> Decimal | None:
    """Read the one-time installation charge from BILLmanager order summary.

    The charge is absent from pricelist prices for some providers, including
    SpaceCore. A complete summary without an installation row means zero fee.
    """
    try:
        doc = json.loads(raw_bytes)["doc"]
        summary = next(node for node in doc["list"] if node.get("$name") == "pricelist_summary")
        rows = summary["elem"]
        if not any("Базовая стоимость" in row.get("label", {}).get("$", "") or
                   "base cost" in row.get("label", {}).get("$", "").lower() for row in rows):
            return None
        for row in rows:
            label = row.get("label", {}).get("$", "").lower()
            if any(word in label for word in ("установка", "установк", "installation", "setup")):
                return Decimal(row["cost"]["price"]["cost"]["$"])
        return Decimal("0")
    except (KeyError, ValueError, TypeError, StopIteration, InvalidOperation):
        return None


def parse_order_default_addons(raw_bytes: bytes) -> dict[str, str]:
    """Read selected addon values from an order form (e.g. auction hardware)."""
    doc = json.loads(raw_bytes).get("doc", {})
    return {
        key: value["$"]
        for key, value in doc.items()
        if re.fullmatch(r"addon_\d+", key)
        and isinstance(value, dict)
        and isinstance(value.get("$"), str)
        and value["$"]
    }


def parse_order_configuration_cost(raw_bytes: bytes) -> Decimal | None:
    """Return default recurring addon cost, or None for an unknown summary."""
    try:
        doc = json.loads(raw_bytes)["doc"]
        summary = next(node for node in doc["list"] if node.get("$name") == "pricelist_summary")
        for row in summary["elem"]:
            label = row.get("label", {}).get("$", "").lower()
            if "конфигурац" in label or "configuration" in label:
                return Decimal(row["cost"]["price"]["cost"]["$"])
        return None
    except (KeyError, ValueError, TypeError, StopIteration, InvalidOperation):
        return None
