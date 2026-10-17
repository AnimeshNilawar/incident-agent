import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.investigation_job import InvestigationJob
from app.services.cloud_tasks import enqueue_investigation

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
        # Keep the job pending so a later dispatcher can retry it.
        job.last_error = str(exc)[:2000]
        job.updated_at = datetime.now(UTC)
        db.commit()

        logger.warning(
            "Could not enqueue investigation job %s; it remains pending",
            job.id,
            exc_info=True,
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
