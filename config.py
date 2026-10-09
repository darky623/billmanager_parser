from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderCredentials(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    provider_id: str  # UUID провайдера из cloudsell БД
    base_url: str
    username: str
    password: SecretStr
    factor: float = 1.3  # price multiplier: final_price = provider_price * factor
    name_prefix: str  # 2-letter prefix for plan names, e.g. "YC" for YaColo, "DC" for Datacheap
    default_network_speed: float = 0.0  # Mbps fallback when provider doesn't publish port speed
    timeout: float | None = None  # optional provider-specific HTTP timeout
    server_types: str = "virtual"  # comma-separated: virtual,dedicated,auction

    @property
    def enabled_server_types(self) -> tuple[str, ...]:
        allowed = {"virtual", "dedicated", "auction"}
        types = tuple(value.strip().lower() for value in self.server_types.split(",") if value.strip())
        if not types or len(set(types)) != len(types) or any(value not in allowed for value in types):
            raise ValueError(f"Invalid server types for {self.base_url}: {self.server_types}")
        return types


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Cloudsell API
    cloudsell_api_url: str
    cloudsell_service_key: SecretStr

    # Providers: comma-separated list of base URLs
    # Credentials per provider: PROVIDER__<INDEX>__BASE_URL, __USERNAME, __PASSWORD
    providers: list[ProviderCredentials] = Field(default_factory=list)

    @field_validator("providers", mode="before")
    @classmethod
    def _parse_providers(cls, v: object) -> object:
        if isinstance(v, dict):
            return [v[k] for k in sorted(v.keys(), key=int)]
        return v

    # Force full re-sync of all plans regardless of snapshot state
    force_full_sync: bool = False

    # Scheduler
    parse_cron: str = "0 2 * * *"  # daily at 02:00

    # Snapshots storage path
    snapshot_dir: str = "snapshots"

    # HTTP timeouts
    provider_timeout: float = 60.0
    api_timeout: float = 600.0


settings = Settings()
