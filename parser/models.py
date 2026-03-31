"""Raw Pydantic models for BILLmanager API responses.

These models map 1-to-1 to the BILLmanager JSON structure.
No business logic — only parsing.
"""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class RawValue(BaseModel):
    """Wrapper around BILLmanager's {$: "..."} pattern."""

    value: str = Field(alias="$", default="")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RawDatacenterValue(BaseModel):
    value: str = Field(alias="$", default="")
    img: str = Field(alias="$img", default="")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RawDatacenter(BaseModel):
    id: RawValue
    value: RawDatacenterValue

    model_config = {"extra": "ignore"}


class RawDetailItem(BaseModel):
    """Single row from 'detail' array: {name: {$: ...}, value: {$: ...}}"""

    name: RawValue
    value: RawValue

    model_config = {"extra": "ignore"}


class RawPriceEntry(BaseModel):
    cost: RawValue
    currency: RawValue
    period: RawValue

    model_config = {"extra": "ignore"}


class RawPrices(BaseModel):
    price: list[RawPriceEntry] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @field_validator("price", mode="before")
    @classmethod
    def ensure_list(cls, v: object) -> list:
        if isinstance(v, dict):
            return [v]
        return v  # type: ignore[return-value]


class RawPlanElem(BaseModel):
    """One VDS plan element from doc.list[0].elem[]"""

    id: RawValue
    title: RawValue = Field(default_factory=RawValue)
    description: RawValue = Field(default_factory=RawValue)
    detail: list[RawDetailItem] = Field(default_factory=list)
    datacenter: RawDatacenter
    prices: RawPrices = Field(default_factory=RawPrices)

    model_config = {"extra": "ignore"}

    @field_validator("detail", mode="before")
    @classmethod
    def ensure_list(cls, v: object) -> list:
        if v is None:
            return []
        if isinstance(v, dict):
            return [v]
        return v  # type: ignore[return-value]

    def detail_as_dict(self) -> dict[str, str]:
        """Convert detail array to flat dict: {name: value}."""
        return {item.name.value: item.value.value for item in self.detail}


class RawSListItem(BaseModel):
    """One item in doc.slist — e.g. datacenter list or OS list."""

    name: str = Field(alias="$name")
    val: list[dict] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RawOsEntry(BaseModel):
    """One OS entry from slist[ostempl].val[]"""

    key: str = Field(alias="$key")
    display_name: str = Field(alias="$", default="")
    value_group: str = Field(alias="$valuegroup", default="")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class ParsedPrice(BaseModel):
    period: int
    cost: Decimal
    currency: str


class ParsedPlan(BaseModel):
    """Cleaned plan data ready for diff and mapping."""

    external_id: int
    name: str
    description_raw: str
    detail: dict[str, str]
    datacenter_id: int
    datacenter_name: str
    prices: list[ParsedPrice]

    # Raw JSON bytes for snapshot comparison
    raw_json: bytes = b""


class ParsedOS(BaseModel):
    external_id: str
    display_name: str
    family: str
