from fastapi import APIRouter, Depends, Response, status

from app.db.session import database_is_reachable

router = APIRouter(prefix="/health", tags=["Health"])


def check_database() -> bool:
    """Dependency wrapper so the readiness probe can be overridden in tests."""
    return database_is_reachable()


@router.get("/live")
def health_live() -> dict[str, str]:
    """Liveness: this process is up and serving HTTP.

    It deliberately touches no dependency, so a slow or briefly unavailable
    database does not make the platform restart an otherwise healthy
    instance.
    """
    return {"status": "alive"}


@router.get("/ready")
def health_ready(
    response: Response,
    database_ok: bool = Depends(check_database),
) -> dict[str, str]:
    """Readiness: can this instance serve traffic right now?

    Returns ``503`` when the database is unreachable, so Cloud Run (or any
    orchestrator) stops routing to the instance while it recovers. Only
    database connectivity is validated here: Cloud Tasks and Google's token
    service are outbound dependencies that are deliberately **not** probed,
    so a healthy response must not be read as "Cloud Tasks works".

    The body never contains the database URL, credentials, or a traceback.
    """
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "unreachable"}

    return {"status": "ready", "database": "ok"}
