from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "local"
    database_url: str = "sqlite:///./incident_agent.db"

    grafana_webhook_username: str = ""
    grafana_webhook_password: str = ""


    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

settings = Settings()