import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.investigation_engine import run_investigation
from app.services.job_lifecycle import (
    ClaimOutcome,
    claim_job,
    complete_job,
    fail_job,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/worker",
    tags=["Investigation Worker"],
)


class InvestigationTaskRequest(BaseModel):
    job_id: uuid.UUID
    incident_id: uuid.UUID


def verify_cloud_tasks_request(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Verify the Google-issued OIDC token sent by Cloud Tasks."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cloud Tasks authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected_email = settings.cloud_tasks_service_account_email
    expected_audience = settings.effective_worker_audience

    if not expected_email or not expected_audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker authentication is not configured",
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
            detail="Invalid worker authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except Exception as exc:
        # Do not log the token or an exception traceback for auth failures.
        logger.warning("Unable to verify Cloud Tasks token")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify worker authentication",
        ) from exc

    if (
        claims.get("email") != expected_email
        or claims.get("email_verified") is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unexpected task service account",
        )


def execute_investigation(
    db: Session,
    job_id: uuid.UUID,
    incident_id: uuid.UUID,
) -> None:
    """Run the investigation engine for a claimed job.

    The engine collects evidence, consults the configured LLM provider,
    validates the structured findings, and persists the report. Any
    failure raises, so the caller's existing claim-fencing records the
    error against this attempt and Cloud Tasks may retry.
    """
    job = db.get(InvestigationJob, job_id)
    incident = db.get(Incident, incident_id) if job else None

    # A successful claim proves the job row exists; the foreign key
    # guarantees the incident does. Guard anyway so a racing deletion
    # surfaces as a recorded failure rather than an AttributeError.
    if job is None or incident is None:
        raise LookupError(
            "Investigation job or incident disappeared after claim"
        )

    run_investigation(db, job=job, incident=incident)


@router.post(
    "/investigate",
    dependencies=[Depends(verify_cloud_tasks_request)],
)
def investigate(
    payload: InvestigationTaskRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    """Claim and process one investigation task."""
    claim = claim_job(
        db,
        job_id=payload.job_id,
        incident_id=payload.incident_id,
    )

    if claim.outcome == ClaimOutcome.NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Investigation job not found",
        )

    # An already completed job is an idempotent redelivery: acknowledge it
    # without running another investigation.
    if claim.outcome == ClaimOutcome.ALREADY_COMPLETED:
        return {"status": "already_completed", "job_id": str(payload.job_id)}

    # Let Cloud Tasks retry while another delivery is processing the job.
    if claim.outcome == ClaimOutcome.ALREADY_RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Investigation is already running",
        )

    # Stop retrying once the application's attempt limit is reached.
    if claim.outcome == ClaimOutcome.ATTEMPT_LIMIT_REACHED:
        return {
            "status": "attempt_limit_reached",
            "job_id": str(payload.job_id),
        }

    if claim.outcome == ClaimOutcome.NOT_CLAIMABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Investigation job cannot be claimed",
        )

    if claim.claim_token is None:
        # Defensive: a successful claim must always carry a fencing token.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Investigation claim token missing",
        )

    try:
        execute_investigation(db, payload.job_id, payload.incident_id)
    except Exception as exc:
        # The engine writes to the database; a failure may have left the
        # session mid-transaction. Roll back (nothing committed by this
        # attempt survives anyway) so fail_job can record the error
        # instead of raising PendingRollbackError. The claim itself was
        # committed by claim_job and is unaffected.
        db.rollback()

        # Record the failure against this specific claim. Cloud Tasks retries
        # on the 5xx response, and the attempt limit bounds how often.
        fail_job(
            db,
            job_id=payload.job_id,
            claim_token=claim.claim_token,
            error=str(exc),
        )

        logger.warning(
            "Investigation job %s failed during processing; will retry",
            payload.job_id,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Investigation failed",
        ) from exc

    completed = complete_job(
        db,
        job_id=payload.job_id,
        claim_token=claim.claim_token,
    )

    if not completed:
        # Another worker reclaimed this job while we were processing it.
        # Do not fight over the row; acknowledge so Cloud Tasks stops
        # redelivering this superseded delivery.
        return {"status": "superseded", "job_id": str(payload.job_id)}

    return {"status": "completed", "job_id": str(payload.job_id)}
