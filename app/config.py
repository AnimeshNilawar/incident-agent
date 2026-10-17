import re

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Schema that owns the agent's tables in PostgreSQL. It is deliberately kept
# out of the host application's ``public`` schema so the two never collide.
#
# This name is FIXED, not free-form: the Alembic migrations hard-code it (and
# ``alembic revision --autogenerate`` always emits the schema as a literal),
# so a different value would make the application and the migrations
# disagree. ``DATABASE_SCHEMA`` is therefore validated rather than merely
# defaulted; changing the schema requires regenerating the migrations. See
# ``docs/database-and-migrations.md``.
DEFAULT_DATABASE_SCHEMA = "incident_agent"

# Environments allowed to fall back to convenience defaults (SQLite and
# automatic table creation for local development). Anything else is treated
# as a deployed environment that must supply an explicit PostgreSQL URL.
LOCAL_ENVIRONMENTS = frozenset({"local", "test"})

# Which routers an instance serves. ``all`` keeps every route in one process
# (local development and the existing test suite). A Cloud Run deployment runs
# the public API and the OIDC-protected worker as separate services from the
# same image, because Cloud Run's IAM authentication is service-wide and
# cannot expose the public routes while requiring authentication only for the
# worker. See ``docs/deployment.md``.
SERVICE_ROLES = frozenset({"all", "api", "worker"})

_SCHEMA_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class Settings(BaseSettings):
    environment: str = "local"
    database_url: str = "sqlite:///./incident_agent.db"

    # Only meaningful for PostgreSQL. Ignored when the URL targets SQLite,
    # which has no notion of a named schema. The value is validated as fixed
    # (see ``_validate_database_schema``).
    database_schema: str = DEFAULT_DATABASE_SCHEMA

    grafana_webhook_username: str = ""
    grafana_webhook_password: str = ""

    # Credentials for the incident-management API (GET/POST
    # ``/api/v1/incidents``). Deliberately a *separate* pair from the Grafana
    # webhook so a leaked webhook secret cannot read or create incidents
    # through the API (and vice versa). Both default to empty, which makes the
    # API fail closed (503) until an operator configures them. See
    # ``docs/deployment.md``.
    incident_api_username: str = ""
    incident_api_password: str = ""

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

    # Which routers this instance serves: ``all`` (default, single process),
    # ``api`` (public incident + Grafana routes) or ``worker`` (Cloud Tasks
    # worker + internal recovery). See ``docs/deployment.md``.
    service_role: str = "all"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    @field_validator("database_schema")
    @classmethod
    def _validate_database_schema(cls, value: str) -> str:
        value = value.strip()

        if not _SCHEMA_IDENTIFIER.match(value):
            raise ValueError(
                "DATABASE_SCHEMA must be a valid SQL identifier "
                "(letters, digits, and underscores, not starting with a digit)"
            )

        if value != DEFAULT_DATABASE_SCHEMA:
            raise ValueError(
                f"the agent schema is fixed to '{DEFAULT_DATABASE_SCHEMA}'. "
                "The Alembic migrations hard-code this schema, so using a "
                "different one requires regenerating the migrations first "
                "(see docs/database-and-migrations.md)."
            )

        return value

    @field_validator("service_role")
    @classmethod
    def _validate_service_role(cls, value: str) -> str:
        value = value.strip().lower()

        if value not in SERVICE_ROLES:
            raise ValueError(
                f"SERVICE_ROLE must be one of {sorted(SERVICE_ROLES)} "
                f"(got '{value}')"
            )

        return value

    @property
    def database_backend(self) -> str:
        """Return the SQLAlchemy backend name (for example ``postgresql``)."""
        return make_url(self.database_url).get_backend_name()

    @property
    def is_postgres(self) -> bool:
        return self.database_backend == "postgresql"

    @property
    def is_local_environment(self) -> bool:
        return self.environment in LOCAL_ENVIRONMENTS

    @property
    def effective_worker_audience(self) -> str:
        """OIDC audience shared by the Cloud Tasks dispatcher and the worker.

        The dispatcher mints the token with this audience and the worker
        verifies it, so both sides must resolve it identically or every task
        is rejected. An explicit ``INVESTIGATION_WORKER_AUDIENCE`` wins;
        otherwise the worker URL is used as a convenience default.

        On Cloud Run the audience is the service's root URL, while the
        request URL includes the endpoint path, so the explicit variable is
        the recommended setting (see ``docs/cloud-tasks-deployment.md``).
        """
        return (
            self.investigation_worker_audience
            or self.investigation_worker_url
        )

    @model_validator(mode="after")
    def _validate_deployed_database(self) -> "Settings":
        """Fail fast instead of silently running SQLite in production.

        Local and test environments keep the convenient SQLite default. Any
        other environment must be given an explicit PostgreSQL URL; otherwise
        the application refuses to start.
        """
        if self.is_local_environment:
            return self

        if not self.database_url.strip():
            raise ValueError(
                "DATABASE_URL must be set when ENVIRONMENT is not one of "
                f"{sorted(LOCAL_ENVIRONMENTS)}"
            )

        if not self.is_postgres:
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL (for example "
                "'postgresql+psycopg://user:password@host:port/dbname') when "
                f"ENVIRONMENT is '{self.environment}'. SQLite is only "
                "allowed in local/test environments."
            )

        return self


def format_settings_error(error: ValidationError) -> str:
    """Summarize a ``Settings`` validation error without echoing its input.

    Pydantic's stock ``ValidationError`` string embeds the raw input mapping,
    which for ``Settings`` includes ``DATABASE_URL`` and therefore its
    password. Rendering only each failing field's location and message keeps a
    startup failure safe to log: ``uvicorn`` prints the import traceback and
    Cloud Logging would otherwise capture the credential.
    """
    problems = []

    for problem in error.errors(include_input=False):
        location = ".".join(
            str(part) for part in problem.get("loc", ())
        ) or "settings"
        problems.append(f"{location}: {problem.get('msg', 'invalid value')}")

    return "; ".join(problems)


try:
    settings = Settings()
except ValidationError as exc:
    # Re-raise with a sanitized message and suppress the chained pydantic error
    # (whose repr contains the raw input, including the database password) so
    # no credential can reach the logs when the application refuses to start.
    raise ValueError(
        f"Invalid configuration: {format_settings_error(exc)}"
    ) from None
