
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.investigation_jobs import create_investigation_job


def test_job_creation_is_idempotent_for_same_incident_event():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)

        with Session(engine) as db:
            incident = Incident(
                fingerprint=f"test-{uuid.uuid4()}",
                title="BackendHighCPU",
                source="grafana",
                severity="high",
                status="received",
                service_name="micuppa-backend-india",
            )
            db.add(incident)
            db.flush()

            first_job, first_created = create_investigation_job(
                db, incident
            )
            second_job, second_created = create_investigation_job(
                db, incident
            )

            db.commit()

            assert first_created is True
            assert second_created is False
            assert first_job.id == second_job.id

            jobs = db.scalars(
                select(InvestigationJob).where(
                    InvestigationJob.incident_id == incident.id
                )
            ).all()

            assert len(jobs) == 1
            assert jobs[0].status == "pending"
            assert jobs[0].attempts == 0
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
