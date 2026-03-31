from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderCredentials(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    base_url: str
    username: str
    password: SecretStr


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

    # Scheduler
    parse_cron: str = "0 2 * * *"  # daily at 02:00

    # Snapshots storage path
    snapshot_dir: str = "snapshots"

    # HTTP timeouts
    provider_timeout: float = 60.0
    api_timeout: float = 30.0


settings = Settings()
