import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.services.investigation_recovery import recover_investigation_jobs

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/internal",
    tags=["Internal Operations"],
)

# Upper bounds keep a single recovery request from scanning the whole table.
MAX_PENDING_AFTER_SECONDS = 86_400
MAX_STALE_RUNNING_SECONDS = 604_800
MAX_BATCH_SIZE = 500


def verify_recovery_request(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the OIDC token of an authorized recovery caller.

    Recovery runs on a separate audience and allowlisted service account
    from the Cloud Tasks worker, so a task-delivery token cannot trigger
    recovery. Authentication fails closed when configuration is missing.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recovery authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected_email = settings.recovery_service_account_email
    expected_audience = settings.recovery_audience

    if not expected_email or not expected_audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recovery authentication is not configured",
        )

    token = authorization.removeprefix("Bearer ").strip()

    try:
        claims = id_token.verify_oauth2_token(
            token,
            GoogleAuthRequest(),
            audience=expected_audience,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recovery authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        # Never log the token or an exception traceback for auth failures.
        logger.warning("Unable to verify recovery authentication token")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify recovery authentication",
        ) from exc

    if (
        claims.get("email") != expected_email
        or claims.get("email_verified") is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unexpected recovery service account",
        )


class RecoveryResponse(BaseModel):
    status: str = "ok"
    pending_found: int
    stale_running_recovered: int
    dispatched: int
    enqueue_failed: int
    attempts_exhausted: int


@router.post(
    "/recovery",
    response_model=RecoveryResponse,
    dependencies=[Depends(verify_recovery_request)],
)
def trigger_recovery(
    db: Annotated[Session, Depends(get_db)],
    pending_after_seconds: int = Query(
        default=30,
        ge=0,
        le=MAX_PENDING_AFTER_SECONDS,
    ),
    stale_running_after_seconds: int | None = Query(
        default=None,
        ge=1,
        le=MAX_STALE_RUNNING_SECONDS,
    ),
    batch_size: int = Query(
        default=100,
        ge=1,
        le=MAX_BATCH_SIZE,
    ),
) -> RecoveryResponse:
    """Trigger bounded recovery of pending and abandoned investigations."""
    # Resolve the lease window at request time so runtime settings changes
    # (and tests that patch settings) take effect; a ``Query`` default would
    # otherwise be frozen from the settings value at import time.
    if stale_running_after_seconds is None:
        stale_running_after_seconds = settings.investigation_job_lease_seconds

    try:
        result = recover_investigation_jobs(
            db,
            pending_after_seconds=pending_after_seconds,
            stale_running_after_seconds=stale_running_after_seconds,
            batch_size=batch_size,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return RecoveryResponse(**result)
