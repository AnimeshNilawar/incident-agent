import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.investigation_job import InvestigationJob
from app.services.investigation_dispatcher import (
    dispatch_investigation_job,
)

LEASE_EXPIRED_ERROR = (
    "Worker lease expired; job returned to pending for recovery"
)
PENDING_RETRY_LIMIT_ERROR = "Retry limit reached before dispatch"
RUNNING_RETRY_LIMIT_ERROR = (
    "Retry limit reached after worker lease expired"
)


def _as_utc(value: datetime) -> datetime:
    """Normalize timestamps from databases with or without timezone support."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fail_exhausted_job(db: Session, job: InvestigationJob, message: str) -> bool:
    """Terminalize a job that has no attempts left (fenced by its state)."""
    result = db.execute(
        update(InvestigationJob)
        .where(
            InvestigationJob.id == job.id,
            InvestigationJob.status == job.status,
            InvestigationJob.attempts == job.attempts,
            InvestigationJob.attempts >= InvestigationJob.max_attempts,
        )
        .values(
            status="failed",
            last_error=message,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )

    return result.rowcount == 1


def _reclaim_expired_lease(
    db: Session,
    job: InvestigationJob,
    lease_cutoff: datetime,
    now: datetime,
) -> bool:
    """Return an abandoned running job to pending, fenced by claim state.

    The conditional ``UPDATE`` re-checks status, attempt token, and the
    lease timestamp so a job that was re-claimed (or legitimately still
    running) in the meantime is left untouched.
    """
    result = db.execute(
        update(InvestigationJob)
        .where(
            InvestigationJob.id == job.id,
            InvestigationJob.status == "running",
            InvestigationJob.attempts == job.attempts,
            InvestigationJob.started_at.is_not(None),
            InvestigationJob.started_at <= lease_cutoff,
        )
        .values(
            status="pending",
            started_at=None,
            last_error=LEASE_EXPIRED_ERROR,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )

    return result.rowcount == 1


def recover_investigation_jobs(
    db: Session,
    *,
    pending_after_seconds: int = 30,
    stale_running_after_seconds: int = 900,
    batch_size: int = 100,
) -> dict[str, int]:
    """Retry eligible pending jobs and reclaim abandoned worker leases.

    Only pending jobs older than ``pending_after_seconds`` are retried, and
    a running job is reclaimed only when its worker lease has clearly
    expired (``started_at`` older than ``stale_running_after_seconds``).
    Attempt counters are never reset, so recovery cannot bypass
    ``max_attempts``.
    """
    if pending_after_seconds < 0:
        raise ValueError("pending_after_seconds cannot be negative")
    if stale_running_after_seconds <= 0:
        raise ValueError("stale_running_after_seconds must be positive")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    now = datetime.now(UTC)
    pending_cutoff = now - timedelta(seconds=pending_after_seconds)
    lease_cutoff = now - timedelta(seconds=stale_running_after_seconds)

    pending_candidates = db.scalars(
        select(InvestigationJob)
        .where(InvestigationJob.status == "pending")
        .order_by(InvestigationJob.updated_at.asc())
        .limit(batch_size)
    ).all()

    pending_ids: list[uuid.UUID] = []
    attempts_exhausted = 0

    for job in pending_candidates:
        if _as_utc(job.updated_at) > pending_cutoff:
            continue

        if job.attempts >= job.max_attempts:
            if _fail_exhausted_job(db, job, PENDING_RETRY_LIMIT_ERROR):
                attempts_exhausted += 1
            continue

        pending_ids.append(job.id)

    running_candidates = db.scalars(
        select(InvestigationJob)
        .where(InvestigationJob.status == "running")
        .order_by(InvestigationJob.started_at.asc())
        .limit(batch_size)
    ).all()

    recovered_running_ids: list[uuid.UUID] = []

    for job in running_candidates:
        # A running job without a claim timestamp cannot be proven
        # abandoned, so it is left for a future, safer recovery path.
        if job.started_at is None:
            continue

        if _as_utc(job.started_at) > lease_cutoff:
            continue

        if job.attempts >= job.max_attempts:
            if _fail_exhausted_job(db, job, RUNNING_RETRY_LIMIT_ERROR):
                attempts_exhausted += 1
            continue

        if _reclaim_expired_lease(db, job, lease_cutoff, now):
            recovered_running_ids.append(job.id)

    # Persist all stale-job recovery before attempting external queue calls.
    db.commit()
    # Drop cached ORM state so the dispatch loop reads the committed truth.
    db.expire_all()

    # Avoid dispatching the same job twice if it appeared in both sets.
    job_ids = list(dict.fromkeys(pending_ids + recovered_running_ids))

    dispatched = 0
    enqueue_failed = 0

    for job_id in job_ids:
        job = db.scalar(
            select(InvestigationJob).where(
                InvestigationJob.id == job_id
            )
        )

        if job is None or job.status != "pending":
            continue

        if dispatch_investigation_job(db, job):
            dispatched += 1
        else:
            enqueue_failed += 1

    return {
        "pending_found": len(pending_ids),
        "stale_running_recovered": len(recovered_running_ids),
        "dispatched": dispatched,
        "enqueue_failed": enqueue_failed,
        "attempts_exhausted": attempts_exhausted,
    }
