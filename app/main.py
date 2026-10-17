import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.models
import app.models.incident  # Registers the model with Base.metadata
from app.api.grafana import router as grafana_router
from app.api.health import router as health_router
from app.api.incidents import router as incidents_router
from app.api.internal import router as internal_router
from app.api.worker import router as worker_router
from app.config import SERVICE_ROLES, settings
from app.db.base import Base
from app.db.session import engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Convenience auto-creation is limited to local SQLite development.
    # Every PostgreSQL environment (including local) is expected to be
    # migrated explicitly with Alembic, so no table is created or altered
    # automatically at startup. Deployed environments additionally refuse to
    # start on SQLite (see ``app.config.Settings``).
    #
    # Migrations are never run here: ``alembic upgrade head`` is an
    # explicit deployment step (see ``docs/database-and-migrations.md``).
    if settings.environment == "local" and not settings.is_postgres:
        Base.metadata.create_all(bind=engine)

    yield


def create_app(service_role: str | None = None) -> FastAPI:
    """Build the ASGI application, mounting only the routes for one role.

    ``SERVICE_ROLE`` selects which routes exist:

    * ``all`` (default) - every route in one process. This is the local
      development and test behaviour, and requires no extra configuration.
    * ``api`` - the public incident API and Grafana webhook only.
    * ``worker`` - the Cloud Tasks worker and internal recovery only.

    Cloud Run authenticates at the **service** level, so one service cannot
    both allow public traffic into the incident API and require
    authentication for the worker. Splitting by role lets the worker service
    be authenticated-only while the API service stays reachable by Grafana,
    and keeps the worker's attack surface to two OIDC-protected routes.

    Unknown roles fail fast at import time rather than starting a service
    with an accidental, unauthenticated route surface.
    """
    role = (service_role or settings.service_role).strip().lower()

    if role not in SERVICE_ROLES:
        raise ValueError(
            f"SERVICE_ROLE must be one of {sorted(SERVICE_ROLES)} "
            f"(got '{role}')"
        )

    application = FastAPI(
        title="Incident Agent",
        description="Automated incident investigation",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Health endpoints are mounted for every role: both services need
    # liveness and readiness probes.
    application.include_router(health_router)

    if role in {"all", "api"}:
        application.include_router(incidents_router)
        application.include_router(grafana_router)

    if role in {"all", "worker"}:
        application.include_router(worker_router)
        application.include_router(internal_router)

    @application.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "incident-agent",
            "status": "running",
        }

    logger.info(
        "Incident Agent app built with SERVICE_ROLE=%s (%d routes)",
        role,
        len(application.routes),
    )

    return application


# The module-level application keeps ``uvicorn app.main:app`` working for
# local development, the container entrypoint, and every existing test.
app = create_app()
