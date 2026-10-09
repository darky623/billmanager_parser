"""Map ParsedPrice list → API price payload list."""

from decimal import Decimal

from parser.models import ParsedPrice

# Supported periods (months). Others are dropped.
_ALLOWED_PERIODS = {1, 3, 6, 12}


def map_prices(prices: list[ParsedPrice], server_type: str = "virtual") -> list[dict]:
    """Return list of dicts ready for PricingPlanPriceAddRequest."""
    result = []
    seen_periods: set[int] = set()

    for price in prices:
        if price.period not in _ALLOWED_PERIODS:
            continue
        if price.period in seen_periods:
            continue
        if price.cost <= Decimal("0"):
            continue
        if server_type == "dedicated" and price.setup_cost is None:
            continue
        if server_type == "auction" and price.setup_cost not in (None, Decimal("0")):
            continue

        seen_periods.add(price.period)
        result.append({
            "period": price.period,
            "provider_price": str(price.cost),
            "currency": price.currency.lower(),
            "setup_fee": str(price.setup_cost or Decimal("0")),
        })

    return result
