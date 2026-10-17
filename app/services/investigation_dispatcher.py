import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.investigation_job import InvestigationJob
from app.services.cloud_tasks import (
    enqueue_investigation,
    is_retryable_error,
)

logger = logging.getLogger(__name__)


def dispatch_investigation_job(
    db: Session,
    job: InvestigationJob,
) -> bool:
    """Queue a pending investigation, retaining failed jobs for retry."""
    if job.status != "pending":
        return False

    try:
        task_name = enqueue_investigation(
            job_id=job.id,
            incident_id=job.incident_id,
        )
    except Exception as exc:
        # A dispatch failure is not an investigation attempt, so it never
        # increments ``attempts``: the job stays ``pending`` and a later
        # recovery sweep retries it. Retryable API failures are expected to
        # clear on their own, so they log at warning level. Permanent
        # failures (missing configuration, denied permissions) never clear
        # without operator action, so they log at error level to be
        # alertable.
        job.last_error = str(exc)[:2000]
        job.updated_at = datetime.now(UTC)
        db.commit()

        if is_retryable_error(exc):
            logger.warning(
                "Could not enqueue investigation job %s; it remains pending",
                job.id,
                exc_info=True,
            )
        else:
            logger.exception(
                "Non-retryable failure enqueuing investigation job %s; it "
                "remains pending until the configuration is fixed",
                job.id,
            )
        return False

    job.status = "queued"
    job.cloud_task_name = task_name
    job.last_error = None
    job.updated_at = datetime.now(UTC)
    db.commit()

    logger.info(
        "Investigation job %s queued as %s",
        job.id,
        task_name,
    )
    return True
