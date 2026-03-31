"""Parse pricelist response into ParsedPlan objects."""

import json
from decimal import Decimal, InvalidOperation

import structlog

from parser.models import ParsedPlan, ParsedPrice, RawPlanElem, RawSListItem

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


def parse_pricelist(raw_bytes: bytes) -> list[ParsedPlan]:
    """Parse raw BILLmanager pricelist JSON into a list of ParsedPlan."""
    data = json.loads(raw_bytes)
    doc = data.get("doc", {})

    list_nodes = doc.get("list", [])
    if not list_nodes:
        log.warning("pricelist: 'list' is empty")
        return []

    # Find the VDS list node (name == "vds" or "pricelist")
    vds_node = next(
        (n for n in list_nodes if n.get("$name") in ("vds", "pricelist")),
        list_nodes[0],
    )
    elems = vds_node.get("elem", [])
    if not elems:
        log.warning("pricelist: no 'elem' found in list node")
        return []

    plans: list[ParsedPlan] = []
    for elem_raw in elems:
        plan = _parse_elem(elem_raw, raw_bytes)
        if plan:
            plans.append(plan)

    log.info("Parsed plans from pricelist", count=len(plans))
    return plans


def _parse_elem(elem_raw: dict, raw_bytes: bytes) -> ParsedPlan | None:
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
            result.append(ParsedPrice(period=period, cost=cost, currency=currency))
        except (ValueError, InvalidOperation) as exc:
            log.warning("Failed to parse price entry", plan_id=external_id, error=str(exc))
    return result


def _all_zero(prices: list[ParsedPrice]) -> bool:
    return all(p.cost == Decimal("0.00") for p in prices)
