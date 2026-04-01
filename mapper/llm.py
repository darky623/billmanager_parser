"""LLM-based hardware feature extraction from BILLmanager plan data.

Mirrors the Java AiServiceImpl / ServerResourcesPromptBuilder logic.
Uses Gemini via Google's OpenAI-compatible endpoint — same as the Java billmanager service:
  base_url = https://generativelanguage.googleapis.com/v1beta/openai
  model    = gemini-2.5-flash
  api_key  = GEMINI_API_KEY env var

- Primary sources: title + description (HTML-cleaned)
- Secondary: structured detail dict fields (when present)
- temperature=0 for deterministic output
"""

import json
import re
from decimal import Decimal

import structlog
from bs4 import BeautifulSoup
from openai import OpenAI
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Known detail field names (Russian) from BILLmanager
_DETAIL_CORES = "Количество процессоров"
_DETAIL_RAM = "Оперативная память"
_DETAIL_DISK = "Дисковое пространство"
_DETAIL_NETWORK = "Входящий трафик"


class ServerFeatures(BaseModel):
    """Mapped server hardware spec — matches cloudsell_api FeaturesAddRequest."""

    processor_name: str | None = None
    cores: int = Field(ge=1, default=1)
    core_frequency: Decimal | None = None
    ram: Decimal = Field(description="RAM in GB")
    ram_type: str = Field(default="DDR4")
    disk: Decimal = Field(description="Disk in GB")
    disk_type: str = Field(default="SSD")
    network_speed: Decimal = Field(default=Decimal("0"), description="Mbps")
    network_limit: Decimal = Field(default=Decimal("0"), description="TB")


_SYSTEM_PROMPT = """\
You are a backend parser. Extract server hardware specifications and return ONLY valid JSON.

Output JSON schema (all values must match these types):
{
  "processor_name": string or null,
  "cores": integer (≥1),
  "core_frequency": number in GHz or null,
  "ram": number in GB (convert: 1 GB = 1024 MB),
  "ram_type": "DDR4" or "DDR3" or "DDR5" (default "DDR4" if unknown),
  "disk": number in GB (convert: 1 GB = 1024 MB, 1 TB = 1024 GB),
  "disk_type": "SSD" or "NVME" or "HDD" (default "SSD" if unknown),
  "network_speed": number in Mbps (e.g. 200Mb/s → 200),
  "network_limit": number in TB (e.g. unlimited or not mentioned → 0)
}

Rules:
- Output ONLY the JSON object, no markdown, no explanation
- Normalize units strictly: 1 GB = 1024 MB, 1 TB = 1024 GB
- Support both Russian and English input
- RAM: look for numbers near "RAM", "МБ", "GB", "DDR", "память"
- Disk: look for numbers near "GB", "ТБ", "SSD", "NVMe", "HDD", "диск"
- Cores: look for "ядр", "core", "vCPU", "CPU", "процессор"
- Network speed: look for "Mb/s", "Gbps", "канал", "скорость"
- If a field is truly unknown: null for strings, 0 for network fields, 1 for cores
- Do NOT confuse disk capacity with RAM
"""


def build_gemini_client(api_key: str) -> OpenAI:
    """Create an OpenAI client pointed at Google's Gemini-compatible endpoint."""
    return OpenAI(
        api_key=api_key,
        base_url=_GEMINI_BASE_URL,
    )


def _clean_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(separator=" ", strip=True)


def _build_user_prompt(title: str, description: str, detail: dict[str, str]) -> str:
    cores_hint = detail.get(_DETAIL_CORES, "")
    ram_hint = detail.get(_DETAIL_RAM, "")
    disk_hint = detail.get(_DETAIL_DISK, "")
    network_hint = detail.get(_DETAIL_NETWORK, "")

    return (
        f'Extract server hardware from the following data:\n\n'
        f'TITLE: "{title}"\n'
        f'DESCRIPTION: "{description}"\n'
        f'DETAIL FIELDS:\n'
        f'- Cores/CPU: "{cores_hint}"\n'
        f'- RAM: "{ram_hint}"\n'
        f'- Disk: "{disk_hint}"\n'
        f'- Network: "{network_hint}"\n'
    )


def extract_features_with_llm(
    client: OpenAI,
    model: str,
    title: str,
    description_raw: str,
    detail: dict[str, str],
    plan_id: int,
) -> ServerFeatures | None:
    """Call Gemini to extract hardware features. Returns None on failure."""
    description = _clean_html(description_raw)
    prompt = _build_user_prompt(title, description, detail)

    log.debug("LLM: extracting features via Gemini", plan_id=plan_id, model=model)

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as exc:
        log.error("LLM API call failed", plan_id=plan_id, error=str(exc))
        return None

    raw_text = (response.choices[0].message.content or "").strip()

    # Strip markdown code fences if present
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        data = json.loads(raw_text)
        features = ServerFeatures.model_validate(data)
        log.debug(
            "LLM: features extracted",
            plan_id=plan_id,
            cores=features.cores,
            ram=str(features.ram),
            disk=str(features.disk),
            disk_type=features.disk_type,
        )
        return features
    except Exception as exc:
        log.error("LLM: failed to parse response", plan_id=plan_id, error=str(exc), raw=raw_text[:300])
        return None
