from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment-level configuration.

    Only infrastructure/bootstrap values live here. Everything that an operator
    tunes at runtime (upstream resolvers, cache behaviour, default action) is
    stored in the database and edited through the API/UI.
    """

    model_config = SettingsConfigDict(env_prefix="MIRENAI_", extra="ignore")

    database_url: str = Field("sqlite:////data/mirenai.db", validation_alias="DATABASE_URL")

    api_listen_address: str = Field("0.0.0.0", validation_alias="API_LISTEN_ADDRESS")  # nosec B104
    api_port: int = Field(8000, validation_alias="API_PORT")

    dns_listen_address: str = Field("0.0.0.0", validation_alias="DNS_LISTEN_ADDRESS")  # nosec B104
    dns_port: int = Field(53, validation_alias="DNS_PORT")

    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")


settings = Settings()
