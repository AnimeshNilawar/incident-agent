"""Concurrency-safe lifecycle operations for investigation jobs.

The database is the single source of truth for who owns a job. Claiming a
job is an atomic conditional ``UPDATE`` that only succeeds while the job is
in a claimable status and still has attempts left. Every state transition
is fenced with the attempt counter, which acts as a monotonically
increasing claim token:

* ``pending -> running`` (claim) increments ``attempts``.
* ``running -> completed`` / ``running -> failed`` only succeeds when the
  ``attempts`` value still matches the value observed at claim time.

A stale worker that keeps running after its lease expired can therefore
never overwrite the state produced by a newer worker, because the newer
claim has already advanced ``attempts`` past the stale worker's token.

The atomic conditional ``UPDATE`` works on both SQLite and PostgreSQL and
does not rely on SQLite's no-op ``with_for_update()``. On PostgreSQL the
``UPDATE`` takes a row lock for the duration of the statement, which is
exactly the serialization the worker needs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.investigation_job import InvestigationJob

# A job can be claimed from any of these non-terminal statuses. ``failed`` is
# claimable so Cloud Tasks can redeliver a transiently failed investigation.
CLAIMABLE_STATUSES: tuple[str, ...] = ("pending", "queued", "failed")

MAXIMUM_ATTEMPTS_ERROR = "Maximum investigation attempts reached"
MAX_ERROR_LENGTH = 2000


class ClaimOutcome(str, Enum):
    """Possible results of trying to claim a job for a worker."""

    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    ALREADY_COMPLETED = "already_completed"
    ALREADY_RUNNING = "already_running"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
    NOT_CLAIMABLE = "not_claimable"


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a claim attempt, including the fencing token on success."""

    outcome: ClaimOutcome
    job_id: uuid.UUID
    # Monotonically increasing claim token, only set when claimed.
    claim_token: int | None = None


def claim_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    incident_id: uuid.UUID,
    now: datetime | None = None,
) -> ClaimResult:
    """Atomically claim a job for this worker.

    The claim only succeeds when the job exists, belongs to ``incident_id``,
    is in a claimable status, and still has attempts remaining. On success
    the returned ``claim_token`` must be presented when completing or
    failing the job.
    """
    claimed_at = now or datetime.now(UTC)

    result = db.execute(
        update(InvestigationJob)
        .where(
            InvestigationJob.id == job_id,
            InvestigationJob.incident_id == incident_id,
            InvestigationJob.status.in_(CLAIMABLE_STATUSES),
            InvestigationJob.attempts < InvestigationJob.max_attempts,
        )
        .values(
            status="running",
            attempts=InvestigationJob.attempts + 1,
            started_at=claimed_at,
            updated_at=claimed_at,
            last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()

    # Force a fresh read so the diagnostic path never reports cached state.
    db.expire_all()

    if result.rowcount == 1:
        job = db.get(InvestigationJob, job_id)
        return ClaimResult(
            outcome=ClaimOutcome.CLAIMED,
            job_id=job_id,
            claim_token=job.attempts if job is not None else None,
        )

    job = db.get(InvestigationJob, job_id)

    if job is None or job.incident_id != incident_id:
        return ClaimResult(outcome=ClaimOutcome.NOT_FOUND, job_id=job_id)

    if job.status == "completed":
        return ClaimResult(
            outcome=ClaimOutcome.ALREADY_COMPLETED,
            job_id=job_id,
            claim_token=job.attempts,
        )

    if job.status == "running":
        return ClaimResult(
            outcome=ClaimOutcome.ALREADY_RUNNING,
            job_id=job_id,
            claim_token=job.attempts,
        )

    if job.attempts >= job.max_attempts:
        # The conditional UPDATE cannot claim the job, so terminalize it to
        # stop Cloud Tasks from redelivering it forever.
        db.execute(
            update(InvestigationJob)
            .where(
                InvestigationJob.id == job_id,
                InvestigationJob.status.in_(CLAIMABLE_STATUSES),
                InvestigationJob.attempts >= InvestigationJob.max_attempts,
            )
            .values(
                status="failed",
                last_error=MAXIMUM_ATTEMPTS_ERROR,
                updated_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()

        return ClaimResult(
            outcome=ClaimOutcome.ATTEMPT_LIMIT_REACHED,
            job_id=job_id,
            claim_token=job.attempts,
        )

    return ClaimResult(
        outcome=ClaimOutcome.NOT_CLAIMABLE,
        job_id=job_id,
        claim_token=job.attempts,
    )


def complete_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    claim_token: int,
    now: datetime | None = None,
) -> bool:
    """Mark a claimed job completed, fenced by the claim token.

    Returns ``True`` only when this worker still owns the job. A stale
    worker whose job was reclaimed returns ``False`` and changes nothing.
    """
    completed_at = now or datetime.now(UTC)

    result = db.execute(
        update(InvestigationJob)
        .where(
            InvestigationJob.id == job_id,
            InvestigationJob.status == "running",
            InvestigationJob.attempts == claim_token,
        )
        .values(
            status="completed",
            completed_at=completed_at,
            updated_at=completed_at,
            last_error=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()

    return result.rowcount == 1


def fail_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    claim_token: int,
    error: str | None,
    now: datetime | None = None,
) -> bool:
    """Mark a claimed job failed, fenced by the claim token.

    Returns ``True`` only when this worker still owns the job. A stale
    worker cannot overwrite the state of a job another worker reclaimed.
    """
    failed_at = now or datetime.now(UTC)
    truncated_error = (error or "")[:MAX_ERROR_LENGTH] or None

    result = db.execute(
        update(InvestigationJob)
        .where(
            InvestigationJob.id == job_id,
            InvestigationJob.status == "running",
            InvestigationJob.attempts == claim_token,
        )
        .values(
            status="failed",
            last_error=truncated_error,
            updated_at=failed_at,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()

    return result.rowcount == 1
