"""Map ParsedPlan → FeaturesPayload for cloudsell API.

Strategy:
1. Try to extract numeric values directly from 'detail' dict (fast path, no LLM cost)
2. If any required field is missing → fall back to LLM extraction
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import structlog
from openai import OpenAI

from mapper.llm import ServerFeatures, extract_features_with_llm
from mapper.location import resolve_location
from parser.models import ParsedPlan

log = structlog.get_logger(__name__)

# detail field names
_DETAIL_CORES = "Количество процессоров"
_DETAIL_RAM = "Оперативная память"
_DETAIL_DISK = "Дисковое пространство"
_DETAIL_NETWORK = "Входящий трафик"

_MB_IN_GB = Decimal("1024")
_GB_IN_TB = Decimal("1024")

_DISK_TYPE_KEYWORDS = {"nvme": "NVME", "ssd": "SSD", "hdd": "HDD"}
_RAM_TYPE_KEYWORDS = {"ddr5": "DDR5", "ddr4": "DDR4", "ddr3": "DDR3"}


def map_features(
    plan: ParsedPlan,
    llm_client: OpenAI,
    llm_model: str,
) -> ServerFeatures | None:
    """
    Try deterministic parsing first.
    If result is incomplete, fall back to LLM.
    """
    fast = _try_fast_parse(plan)
    if fast is not None:
        log.debug("Features: fast-parsed (no LLM)", plan_id=plan.external_id)
        return fast

    log.debug("Features: fast-parse incomplete, calling LLM", plan_id=plan.external_id)
    return extract_features_with_llm(
        client=llm_client,
        model=llm_model,
        title=plan.name,
        description_raw=plan.description_raw,
        detail=plan.detail,
        plan_id=plan.external_id,
    )


def get_location_fields(plan: ParsedPlan) -> tuple[str | None, str | None]:
    """Return (location_code, location_raw)."""
    raw = plan.datacenter_name
    return resolve_location(raw), raw


# ---------------------------------------------------------------------------
# Fast deterministic parser
# ---------------------------------------------------------------------------

def _try_fast_parse(plan: ParsedPlan) -> ServerFeatures | None:
    """
    Attempt to extract features purely from the detail dict.
    Returns None if any required field is missing or unparseable.
    """
    d = plan.detail
    try:
        cores = _parse_integer(d.get(_DETAIL_CORES, ""))
        ram_gb = _parse_size_to_gb(d.get(_DETAIL_RAM, ""))
        disk_gb = _parse_size_to_gb(d.get(_DETAIL_DISK, ""))
    except (ValueError, InvalidOperation):
        return None

    if cores is None or ram_gb is None or disk_gb is None:
        return None

    disk_type = _detect_disk_type(d.get(_DETAIL_DISK, "") + " " + plan.description_raw)
    ram_type = _detect_ram_type(plan.description_raw)
    network_speed = _parse_network_speed(d.get(_DETAIL_NETWORK, "") + " " + plan.description_raw)

    processor_name = _parse_processor_name(plan.description_raw)

    return ServerFeatures(
        processor_name=processor_name,
        cores=cores,
        core_frequency=None,
        ram=ram_gb,
        ram_type=ram_type or "DDR4",
        disk=disk_gb,
        disk_type=disk_type or "SSD",
        network_speed=network_speed or Decimal("0"),
        network_limit=Decimal("0"),
    )


def _parse_integer(text: str) -> int | None:
    """Extract first integer from a string like '1 Шт.' or '2 cores'."""
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _parse_size_to_gb(text: str) -> Decimal | None:
    """
    Parse storage/memory string to GB.
    Examples: '1024 МБ' → 1.0, '20000 МБ' → ~19.5, '20 GB' → 20, '1 TB' → 1024
    """
    if not text:
        return None
    text_lower = text.lower()

    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not m:
        return None

    raw_num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw_num)
    except InvalidOperation:
        return None

    if any(k in text_lower for k in ("мб", "mb", "мегаб")):
        return _round_to_standard_gb((value / _MB_IN_GB).quantize(Decimal("0.001")))
    if any(k in text_lower for k in ("тб", "tb", "терабайт")):
        return value * _GB_IN_TB
    if any(k in text_lower for k in ("гб", "gb", "гигаб")):
        return value

    # No unit — assume MB if small number, GB otherwise
    if value < 128:
        return value  # likely already GB
    return _round_to_standard_gb((value / _MB_IN_GB).quantize(Decimal("0.001")))


def _round_to_standard_gb(value: Decimal) -> Decimal:
    """Round to nearest integer GB if within 5% tolerance (handles decimal vs binary GB)."""
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rounded > 0 and abs(value - rounded) / rounded < Decimal("0.05"):
        return rounded
    return value


def _parse_processor_name(text: str) -> str | None:
    """Extract CPU model from description text."""
    if not text:
        return None
    clean = re.sub(r'<[^>]+>', ' ', text)
    m = re.search(
        r'(Intel\s+(?:Xeon|Core\s+i\d|Pentium|Celeron)[^\s,<]{0,30}|'
        r'AMD\s+(?:EPYC|Ryzen|Opteron)[^\s,<]{0,30})',
        clean,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _detect_disk_type(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, disk_type in _DISK_TYPE_KEYWORDS.items():
        if keyword in text_lower:
            return disk_type
    return None


def _detect_ram_type(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, ram_type in _RAM_TYPE_KEYWORDS.items():
        if keyword in text_lower:
            return ram_type
    return None


def _parse_network_speed(text: str) -> Decimal | None:
    """Parse network speed to Mbps. '200Mb/s' → 200, '1 Gbps' → 1000."""
    if not text:
        return None

    clean = re.sub(r'<[^>]+>', ' ', text)

    gbps_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:gbps|гбит(?:/с|/s)?)",
        clean,
        re.IGNORECASE,
    )
    if gbps_match:
        return Decimal(gbps_match.group(1).replace(",", ".")) * 1000

    mbps_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:mb/?s|мбит(?:/с|/s)?|мб/с|mbps)",
        clean,
        re.IGNORECASE,
    )
    if mbps_match:
        return Decimal(mbps_match.group(1).replace(",", "."))

    return None
