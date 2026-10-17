from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob


def create_investigation_job(
    db: Session,
    incident: Incident,
) -> tuple[InvestigationJob, bool]:
    """Create an investigation job unless this event already has one."""
    idempotency_key = (
        f"incident:{incident.id}:event:{incident.last_seen_at.isoformat()}"
    )

    existing = db.scalar(
        select(InvestigationJob).where(
            InvestigationJob.idempotency_key == idempotency_key
        )
    )

    if existing is not None:
        return existing, False

    job = InvestigationJob(
        incident_id=incident.id,
        idempotency_key=idempotency_key,
        status="pending",
    )

    db.add(job)
    db.flush()

    return job, True
