"""Map ParsedPrice list → API price payload list."""

from decimal import Decimal

from parser.models import ParsedPrice


# Supported periods (months). Others are dropped.
_ALLOWED_PERIODS = {1, 3, 6, 12}


def map_prices(prices: list[ParsedPrice]) -> list[dict]:
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

        seen_periods.add(price.period)
        result.append({
            "period": price.period,
            "provider_price": str(price.cost),
            "currency": price.currency.upper(),
        })

    return result
