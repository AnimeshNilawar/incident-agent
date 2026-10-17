
import logging
import uuid
from unittest.mock import patch

import pytest
from google.api_core.exceptions import PermissionDenied, ServiceUnavailable
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


@pytest.fixture
def dispatcher_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(bind=engine)

    try:
        with Session(engine) as db:
            yield db
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_dispatch_retries_transient_failure_on_the_next_attempt(
    dispatcher_db,
):
    job = make_pending_job(dispatcher_db)

    with patch(
        "app.services.investigation_dispatcher.enqueue_investigation",
        side_effect=ServiceUnavailable("Cloud Tasks unavailable"),
    ):
        assert dispatch_investigation_job(dispatcher_db, job) is False

    dispatcher_db.refresh(job)
    assert job.status == "pending"
    assert "Cloud Tasks unavailable" in job.last_error
    assert job.cloud_task_name is None

    # A later sweep can queue the still-pending job.
    with patch(
        "app.services.investigation_dispatcher.enqueue_investigation",
        return_value="projects/test/locations/asia-south1/"
        "queues/incident-investigations/tasks/retried-task",
    ):
        assert dispatch_investigation_job(dispatcher_db, job) is True

    dispatcher_db.refresh(job)
    assert job.status == "queued"
    assert job.cloud_task_name.endswith("retried-task")
    assert job.last_error is None


def test_dispatch_logs_retryable_failure_at_warning_level(
    dispatcher_db, caplog
):
    job = make_pending_job(dispatcher_db)

    with (
        caplog.at_level(
            logging.DEBUG, logger="app.services.investigation_dispatcher"
        ),
        patch(
            "app.services.investigation_dispatcher.enqueue_investigation",
            side_effect=ServiceUnavailable("Cloud Tasks unavailable"),
        ),
    ):
        assert dispatch_investigation_job(dispatcher_db, job) is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "it remains pending" in r.getMessage() for r in warnings
    )


def test_dispatch_logs_permanent_failure_at_error_level(
    dispatcher_db, caplog
):
    job = make_pending_job(dispatcher_db)

    with (
        caplog.at_level(
            logging.DEBUG, logger="app.services.investigation_dispatcher"
        ),
        patch(
            "app.services.investigation_dispatcher.enqueue_investigation",
            side_effect=PermissionDenied("missing cloudtasks.tasks.create"),
        ),
    ):
        assert dispatch_investigation_job(dispatcher_db, job) is False

    # Permanent failures cannot clear on their own, so they are loggable
    # as errors for alerting while the job stays pending for recovery.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("Non-retryable failure" in r.getMessage() for r in errors)

    dispatcher_db.refresh(job)
    assert job.status == "pending"
