
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob


def test_investigation_job_can_be_persisted():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)

        with Session(engine) as db:
            incident = Incident(
                fingerprint="test-investigation-001",
                title="BackendHighCPU",
                source="grafana",
                severity="high",
                status="received",
                service_name="micuppa-backend-india",
            )
            db.add(incident)
            db.flush()

            job = InvestigationJob(
                incident_id=incident.id,
                idempotency_key=f"investigate:{uuid.uuid4()}",
            )
            db.add(job)
            db.commit()

            saved_job = db.scalar(
                select(InvestigationJob).where(
                    InvestigationJob.id == job.id
                )
            )

            assert saved_job is not None
            assert saved_job.incident_id == incident.id
            assert saved_job.status == "pending"
            assert saved_job.attempts == 0
            assert saved_job.max_attempts == 3
            assert saved_job.last_error is None
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
