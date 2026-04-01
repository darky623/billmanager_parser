"""Parse OS list response for a specific plan."""

import json

import structlog

from parser.models import ParsedOS, RawOsEntry

log = structlog.get_logger(__name__)

_FALLBACK_FAMILY = "Other"


def parse_os_list(raw_bytes: bytes, plan_id: int) -> list[ParsedOS]:
    """Parse BILLmanager v2.vds.order.param response and extract OS entries."""
    data = json.loads(raw_bytes)
    doc = data.get("doc", {})

    slist = doc.get("slist", [])
    if not slist:
        log.warning("OS list: 'slist' is empty", plan_id=plan_id)
        return []

    ostempl_node = next(
        (item for item in slist if isinstance(item, dict) and item.get("$name") == "ostempl"),
        None,
    )
    if not ostempl_node:
        log.warning("OS list: no 'ostempl' node in slist", plan_id=plan_id)
        return []

    val = ostempl_node.get("val", [])
    os_list: list[ParsedOS] = []

    for raw_entry in val:
        try:
            entry = RawOsEntry.model_validate(raw_entry)
        except Exception as exc:
            log.warning("Failed to parse OS entry", plan_id=plan_id, error=str(exc), entry=raw_entry)
            continue

        if not entry.key or not entry.display_name:
            log.debug("Skipping OS entry: missing key or name", plan_id=plan_id, entry=raw_entry)
            continue

        raw_group = entry.value_group.strip()
        family = raw_group if raw_group and raw_group != _FALLBACK_FAMILY else _detect_family(entry.display_name)

        os_list.append(ParsedOS(
            external_id=entry.key,
            display_name=entry.display_name,
            family=family,
        ))

    log.info("Parsed OS entries", plan_id=plan_id, count=len(os_list))
    return os_list


def _detect_family(name: str) -> str:
    """Guess OS family from display name when $valuegroup is absent."""
    name_lower = name.lower()
    families = [
        ("ubuntu", "Ubuntu"),
        ("debian", "Debian"),
        ("almalinux", "AlmaLinux"),
        ("rockylinux", "Rocky Linux"),
        ("rocky", "Rocky Linux"),
        ("centos", "CentOS"),
        ("fedora", "Fedora"),
        ("opensuse", "openSUSE"),
        ("archlinux", "Arch Linux"),
        ("gentoo", "Gentoo"),
        ("windows", "Windows"),
        ("freebsd", "FreeBSD"),
        ("oracle", "Oracle Linux"),
        ("astra", "Astra Linux"),
        ("altlinux", "ALT Linux"),
    ]
    for keyword, label in families:
        if keyword in name_lower:
            return label
    return _FALLBACK_FAMILY
