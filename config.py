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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Gemini (via OpenAI-compatible endpoint, same as Java billmanager service)
    gemini_api_key: SecretStr
    gemini_model: str = "gemini-2.5-flash"

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

    # Scheduler
    parse_cron: str = "0 2 * * *"  # daily at 02:00

    # Snapshots storage path
    snapshot_dir: str = "snapshots"

    # HTTP timeouts
    provider_timeout: float = 60.0
    api_timeout: float = 30.0


settings = Settings()
