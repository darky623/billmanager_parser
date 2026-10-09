"""Map ParsedPlan → FeaturesPayload for cloudsell API.

Extract numeric values directly from the BILLmanager detail fields.
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import structlog

from mapper.location import resolve_location
from mapper.models import ServerFeatures
from parser.models import ParsedPlan

log = structlog.get_logger(__name__)

# detail field names
_DETAIL_CORES = "Количество процессоров"
_DETAIL_RAM = "Оперативная память"
_DETAIL_DISK = "Дисковое пространство"
_DETAIL_NETWORK_SPEED = "Скорость порта"    # actual port speed (Мбит/с, Гбит/с)
_DETAIL_NETWORK_SPEED_ALT = "Канал"          # alternative: some providers
_DETAIL_NETWORK_SPEED_WIDTH = "Ширина канала"  # YaColo: "100 Mбит/сек"
_DETAIL_NETWORK_LIMIT = "Входящий трафик"    # traffic limit in GB/TB, not speed

_MB_IN_GB = Decimal("1024")
_GB_IN_TB = Decimal("1024")

_DISK_TYPE_KEYWORDS = {"nvme": "NVME", "ssd": "SSD", "hdd": "HDD"}
_RAM_TYPE_KEYWORDS = {"ddr5": "DDR5", "ddr4": "DDR4", "ddr3": "DDR3"}


def map_features(plan: ParsedPlan) -> ServerFeatures | None:
    """Parse hardware fields without an external AI service."""
    fast = _try_fast_parse(plan)
    if fast is not None:
        log.debug("Features parsed", plan_id=plan.external_id)
        return fast

    log.warning("Required hardware fields missing; skipping plan", plan_id=plan.external_id)
    return None


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
    description = re.sub(r"<[^>]+>", " ", plan.description_raw)
    selected_configuration = _selected_configuration(plan.raw_order_param)
    try:
        cores = _parse_integer(d.get(_DETAIL_CORES, ""))
        ram_gb = _parse_size_to_gb(d.get(_DETAIL_RAM, ""))
        disk_gb = _parse_size_to_gb(d.get(_DETAIL_DISK, ""))
        if plan.server_type != "virtual":
            cores = _physical_cores(plan.name) or _physical_cores_detail(d.get("Процессор", "")) or cores
            ram_gb = ram_gb or _physical_ram(plan.name) or _physical_ram(selected_configuration)
            disk_gb = (
                _physical_disk_detail(d) or _physical_disk(plan.name)
                or _physical_disk(selected_configuration) or disk_gb
            )
        if cores is None:
            cores = _parse_integer(_description_field(description, r"(?:Процессор|CPU|Cores?)", r"\d+"))
        if ram_gb is None:
            ram_gb = _parse_size_to_gb(
                _description_field(description, r"(?:Память|RAM)", r"\d+(?:[.,]\d+)?\s*(?:[TGMТГМ][BbБб]?)")
            )
        if disk_gb is None:
            disk_gb = _parse_size_to_gb(
                _description_field(description, r"(?:Диск|Disk|SSD|HDD|NVMe)", r"\d+(?:[.,]\d+)?\s*(?:[TGMТГМ][BbБб]?)")
            )
    except (ValueError, InvalidOperation):
        return None

    if cores is None or ram_gb is None or disk_gb is None:
        return None

    disk_type = _detect_disk_type(
        d.get(_DETAIL_DISK, "") + " " + plan.description_raw + " " + plan.name + " " + selected_configuration
    )
    ram_type = _detect_ram_type(plan.description_raw + " " + plan.name)

    # Collect speed hints: dedicated speed fields first, then description
    speed_text = " ".join(filter(None, [
        d.get(_DETAIL_NETWORK_SPEED, ""),
        d.get(_DETAIL_NETWORK_SPEED_ALT, ""),
        d.get(_DETAIL_NETWORK_SPEED_WIDTH, ""),
        plan.description_raw,
        plan.name,
    ]))
    network_speed = _parse_network_speed(speed_text)

    processor_name = _parse_processor_name(plan.description_raw + " " + plan.name)
    if processor_name is None and plan.server_type != "virtual":
        processor_name = plan.name.split("/")[0].strip()[:50]
    network_limit = _parse_network_limit(d.get(_DETAIL_NETWORK_LIMIT, ""))

    return ServerFeatures(
        processor_name=processor_name,
        cores=cores,
        core_frequency=None,
        ram=ram_gb,
        ram_type=ram_type or "DDR4",
        disk=disk_gb,
        disk_type=disk_type or "SSD",
        network_speed=network_speed or Decimal("0"),
        network_limit=network_limit,
    )


def _physical_cores(name: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:ядер|ядра|cores?)\b", name, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(\d+)c\s*/\s*\d+t\b", name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _physical_cores_detail(processor: str) -> int | None:
    match = re.search(r"(\d+)\s*cores?\b", processor, re.IGNORECASE)
    if not match:
        return None
    count = int(match.group(1))
    if count <= 16 and re.search(r"\b2\s*[xх×]", processor, re.IGNORECASE):
        count *= 2
    return count


def _physical_ram(name: str) -> Decimal | None:
    match = re.search(r"RAM\s*(\d+(?:[.,]\d+)?)\s*(?:ГБ|GB)", name, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ГБ|GB)\s*(?:DDR\d*|RAM)\b", name, re.IGNORECASE)
    return Decimal(match.group(1).replace(",", ".")) if match else None


def _physical_disk(name: str) -> Decimal | None:
    values = []
    for match in re.finditer(
        r"(?:(\d+)\s*[xх×]\s*)?(\d+(?:[.,]\d+)?)\s*(ГБ|GB|ТБ|TB)\s*(?:[A-Z.]{0,12})?(?:NVMe|SSD|HDD)",
        name, re.IGNORECASE,
    ):
        multiplier = int(match.group(1) or 1)
        size = Decimal(match.group(2).replace(",", "."))
        if match.group(3).lower() in ("тб", "tb"):
            size *= 1024
        values.append(size * multiplier)
    return sum(values, Decimal("0")) or None


def _physical_disk_detail(detail: dict[str, str]) -> Decimal | None:
    sizes = []
    for key, value in detail.items():
        if not key.lower().startswith(("жесткий диск", "диск ")):
            continue
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(ГБ|GB|ТБ|TB)\b", value, re.IGNORECASE)
        if match:
            size = Decimal(match.group(1).replace(",", "."))
            sizes.append(size * (1024 if match.group(2).lower() in ("тб", "tb") else 1))
    return sum(sizes, Decimal("0")) or None


def _selected_configuration(raw_order_param: bytes) -> str:
    if not raw_order_param:
        return ""
    try:
        doc = json.loads(raw_order_param)["doc"]
    except (ValueError, KeyError):
        return ""
    selected = []
    for item in doc.get("slist", []):
        key = item.get("$name", "")
        if not re.fullmatch(r"addon_\d+", key):
            continue
        value = doc.get(key, {}).get("$")
        selected.extend(option.get("$", "") for option in item.get("val", []) if option.get("$key") == value)
    return " ".join(selected)


def _description_field(description: str, label: str, value: str) -> str:
    """Extract a labelled hardware value from the provider's free-text description."""
    match = re.search(rf"\b{label}\s*[:=-]?\s*({value})", description, re.IGNORECASE)
    return match.group(1) if match else ""


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

    if any(k in text_lower for k in ("миб", "мб", "mb", "мегаб", "mib")):
        return _round_to_standard_gb((value / _MB_IN_GB).quantize(Decimal("0.001")))
    if any(k in text_lower for k in ("тиб", "тб", "tb", "терабайт", "tib")):
        return value * _GB_IN_TB
    if any(k in text_lower for k in ("гиб", "гб", "gb", "гигаб", "gib")):
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
    """Parse network speed to Mbps.

    Examples:
      '200Mb/s' → 200, '1 Gbps' → 1000, '1 Гбит' → 1000,
      '100 Мбит' → 100, '1G' → 1000, '10G port' → 10000,
      'канал 1 Гбит' → 1000
    """
    if not text:
        return None

    clean = re.sub(r'<[^>]+>', ' ', text)

    # Gbps patterns — with and without /с or /s suffix
    gbps_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:gbps|гбит(?:/с(?:\.|)?|/s)?)",
        clean,
        re.IGNORECASE,
    )
    if gbps_match:
        return Decimal(gbps_match.group(1).replace(",", ".")) * 1000

    # Mbps patterns — with and without /с or /s suffix
    # Handles mixed Latin/Cyrillic like YaColo "100 Mбит/сек" (Latin M + Cyrillic бит)
    mbps_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:mb/?s|mbps|[МмMm]бит(?:/с(?:ек)?|/s)?|мб/с)",
        clean,
        re.IGNORECASE,
    )
    if mbps_match:
        return Decimal(mbps_match.group(1).replace(",", "."))

    # Short G/Gbit suffix — e.g. '1G', '10G port', '1 Gbit'
    g_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:gbit\b|[Gg]бит\b|[Gg]\b)",
        clean,
    )
    if g_match:
        return Decimal(g_match.group(1).replace(",", ".")) * 1000

    # Short M/Mbit suffix — e.g. '100M', '100 Mbit'
    m_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:mbit\b|[Мм]бит\b|[Mm]\b)",
        clean,
    )
    if m_match:
        return Decimal(m_match.group(1).replace(",", "."))

    return None


def _parse_network_limit(text: str) -> Decimal:
    """Parse traffic limit from detail field to TB.

    Examples: 'безлимит' → 0, '100 ГБ' → 0.097, '1 ТБ' → 1, '10 TB' → 10
    Returns 0 for unlimited or unparseable.
    """
    if not text:
        return Decimal("0")
    text_lower = text.lower()

    unlimited_keywords = ("безлимит", "unlim", "unlimited", "∞", "не ограничен")
    if any(k in text_lower for k in unlimited_keywords):
        return Decimal("0")

    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)", text)
    if not m:
        return Decimal("0")

    raw_num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw_num)
    except InvalidOperation:
        return Decimal("0")

    if any(k in text_lower for k in ("тиб", "тб", "tb", "тера", "tib")):
        return value
    if any(k in text_lower for k in ("гиб", "гб", "gb", "гига", "gib")):
        return (value / _GB_IN_TB).quantize(Decimal("0.001"))

    return Decimal("0")
