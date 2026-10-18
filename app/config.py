import re

from pydantic import Field, ValidationError, field_validator, model_validator
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

    # --- LLM provider (investigation engine) --------------------------------
    # The engine talks to any OpenAI-compatible chat-completions endpoint
    # (OpenAI, a relay, or a self-hosted server). The key is read only from
    # the environment / .env and is never logged; an empty key makes the
    # engine fail a job with an explicit "provider not configured" error
    # instead of silently producing an empty report.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    # Hard bounds so one investigation cannot occupy a worker indefinitely.
    llm_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Backend log collection --------------------------------------------
    # Configures how the investigation engine collects backend logs.
    # The collector is a pluggable component:
    #   "local" - an in-memory source (tests/local development; empty by
    #             default, which records an explicit gap).
    #   "http"  - any HTTP endpoint returning JSON log entries; requires
    #             BACKEND_LOGS_URL (and optionally BACKEND_LOGS_AUTH_TOKEN).
    #   "gcp"   - Google Cloud Logging (Cloud Run service logs); requires
    #             GCP_PROJECT_ID and Application Default Credentials on
    #             the deployed worker (roles/logging.viewer, read-only).
    #             See app/services/gcp_logging.py.
    #   "none"  - collection is explicitly disabled; recorded as a gap.
    # Unknown values are rejected at startup rather than silently
    # disabling evidence collection.
    backend_logs_source: str = "local"
    # Base URL of the HTTP log source (only used when source="http").
    # Left empty on purpose: no production endpoint is assumed. When the
    # source is "http" and this is empty, the collector records an
    # explicit "missing" gap instead of inventing log content.
    backend_logs_url: str = ""
    # Optional bearer token for the HTTP log source, read only from the
    # environment / .env and never logged or stored in evidence.
    backend_logs_auth_token: str = ""
    # Optional severity floor (e.g. "warning", "error"). Empty keeps
    # every level. Entries with an unclassifiable level are always kept.
    backend_logs_min_level: str = ""
    backend_logs_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    backend_logs_max_entries: int = Field(default=100, ge=1, le=500)
    backend_logs_max_bytes: int = Field(default=100_000, ge=1024, le=1_000_000)
    backend_logs_window_minutes_before: int = Field(default=10, ge=0, le=60)
    backend_logs_window_minutes_after: int = Field(default=10, ge=0, le=60)
    backend_logs_redact_patterns: str = ""  # comma-separated regex patterns
    # Only used when BACKEND_LOGS_SOURCE=gcp. Optional comma-separated
    # "incident_service=cloud_run_service" pairs for when an incident's
    # service name differs from the Cloud Run service name, e.g.
    # "backend=micuppa-backend-india". Empty uses the incident's own
    # service name as the Cloud Run service name. The GCP project comes
    # from GCP_PROJECT_ID (shared with Cloud Tasks dispatch) and
    # credentials come from Application Default Credentials; nothing is
    # hard-coded here.
    backend_logs_gcp_service_map: str = ""

    # --- PostHog frontend error collection ---------------------------------
    # Host (e.g. https://us.i.posthog.com or https://eu.i.posthog.com),
    # the numeric project ID, and a personal API token with read access
    # to events. All three must be set; with any of them empty the
    # collector records an explicit "missing" gap instead of inventing
    # frontend errors, so an investigation continues honestly with the
    # evidence it does have. The token is read ONLY from the environment
    # / .env - never committed, never logged, never stored in evidence.
    # Bounded time window, entry/byte limits, the severity floor and
    # redaction reuse the BACKEND_LOGS_* settings above, so one
    # investigation stays bounded no matter which evidence source runs.
    posthog_host: str = ""
    posthog_project_id: str = ""
    posthog_api_key: str = ""

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

    @field_validator("backend_logs_source")
    @classmethod
    def _validate_backend_logs_source(cls, value: str) -> str:
        value = value.strip().lower()

        if value not in {"local", "http", "gcp", "none"}:
            raise ValueError(
                "BACKEND_LOGS_SOURCE must be one of "
                "{'local', 'http', 'gcp', 'none'} "
                f"(got '{value}')"
            )

        return value

    @field_validator("posthog_host")
    @classmethod
    def _validate_posthog_host(cls, value: str) -> str:
        value = value.strip().rstrip("/")

        if value and not value.startswith(("https://", "http://")):
            raise ValueError(
                "POSTHOG_HOST must be empty or an http(s) URL "
                f"(got '{value}')"
            )

        return value

    @field_validator("posthog_project_id")
    @classmethod
    def _validate_posthog_project_id(cls, value: str) -> str:
        value = value.strip()

        if value and not value.isdigit():
            raise ValueError(
                "POSTHOG_PROJECT_ID must be empty or the numeric "
                f"PostHog project ID (got '{value}')"
            )

        return value

    @field_validator("backend_logs_min_level")
    @classmethod
    def _validate_backend_logs_min_level(cls, value: str) -> str:
        value = value.strip().lower()

        if value and value not in {
            "debug",
            "info",
            "notice",
            "warn",
            "warning",
            "error",
            "critical",
            "fatal",
        }:
            raise ValueError(
                "BACKEND_LOGS_MIN_LEVEL must be empty or one of the "
                "supported severity levels (got "
                f"'{value}')"
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
    def llm_configured(self) -> bool:
        """Whether an LLM provider credential has been supplied.

        The engine refuses to investigate (and fails the job with an
        explicit error) when this is ``False``, rather than inventing an
        unconstrained report with no model behind it.
        """
        return bool(self.llm_api_key.strip())

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
