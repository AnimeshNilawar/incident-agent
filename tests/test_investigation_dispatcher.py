
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.investigation_dispatcher import (
    dispatch_investigation_job,
)


def make_pending_job(db: Session) -> InvestigationJob:
    incident = Incident(
        fingerprint=f"dispatcher-test-{uuid.uuid4()}",
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
        idempotency_key=f"dispatcher:{uuid.uuid4()}",
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    return job


def test_dispatch_success_marks_job_queued():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)

        with Session(engine) as db:
            job = make_pending_job(db)

            with patch(
                "app.services.investigation_dispatcher.enqueue_investigation",
                return_value="projects/test/locations/asia-south1/"
                "queues/incident-investigations/tasks/test-task",
            ) as enqueue:
                result = dispatch_investigation_job(db, job)

            assert result is True
            enqueue.assert_called_once_with(
                job_id=job.id,
                incident_id=job.incident_id,
            )

            db.refresh(job)
            assert job.status == "queued"
            assert job.cloud_task_name.endswith("test-task")
            assert job.last_error is None
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_dispatch_failure_keeps_job_pending_for_retry():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)

        with Session(engine) as db:
            job = make_pending_job(db)

            with patch(
                "app.services.investigation_dispatcher.enqueue_investigation",
                side_effect=RuntimeError("Cloud Tasks unavailable"),
            ):
                result = dispatch_investigation_job(db, job)

            assert result is False

            db.refresh(job)
            assert job.status == "pending"
            assert job.last_error == "Cloud Tasks unavailable"
            assert job.cloud_task_name is None
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_dispatch_skips_jobs_that_are_not_pending():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)

        with Session(engine) as db:
            job = make_pending_job(db)
            job.status = "queued"
            db.commit()

            with patch(
                "app.services.investigation_dispatcher.enqueue_investigation"
            ) as enqueue:
                result = dispatch_investigation_job(db, job)

            assert result is False
            enqueue.assert_not_called()
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
