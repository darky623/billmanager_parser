"""Hardware fields sent to the CloudSell API."""

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class ServerFeatures(BaseModel):
    """Mapped server hardware spec matching FeaturesAddRequest."""

    processor_name: str | None = None
    cores: int = Field(ge=1, default=1)
    core_frequency: Decimal | None = None
    ram: Decimal = Field(description="RAM in GB")
    ram_type: str = Field(default="DDR4")
    disk: Decimal = Field(description="Disk in GB")
    disk_type: str = Field(default="SSD")
    network_speed: Decimal = Field(default=Decimal("0"), description="Mbps")
    network_limit: Decimal = Field(default=Decimal("0"), description="TB")

    @field_validator("network_speed", "network_limit", mode="before")
    @classmethod
    def _coerce_none_to_zero(cls, value: object) -> object:
        return value if value is not None else Decimal("0")
