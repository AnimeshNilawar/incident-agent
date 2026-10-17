from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "local"
    database_url: str = "sqlite:///./incident_agent.db"

    grafana_webhook_username: str = ""
    grafana_webhook_password: str = ""

    gcp_project_id: str = ""
    cloud_tasks_location: str = "asia-south1"
    cloud_tasks_queue: str = "incident-investigations"

    investigation_worker_url: str = ""
    cloud_tasks_service_account_email: str = ""
    investigation_worker_audience: str = ""

    # Recovery is triggered by a separate authorized caller (for example a
    # Cloud Scheduler job). It uses its own audience and allowlisted service
    # account so worker delivery credentials cannot trigger recovery.
    recovery_service_account_email: str = ""
    recovery_audience: str = ""

    # How long a worker's claim on a job is considered valid. A running job
    # whose claim is older than this is treated as abandoned by recovery.
    investigation_job_lease_seconds: int = 900


    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

settings = Settings()