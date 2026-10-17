import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.investigation_recovery import recover_investigation_jobs


@pytest.fixture
def db_session():
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


def create_job(
    db: Session,
    *,
    status: str,
    updated_at: datetime | None = None,
    started_at: datetime | None = None,
    attempts: int = 0,
    max_attempts: int = 3,
) -> InvestigationJob:
    incident = Incident(
        fingerprint=f"recovery-test-{uuid.uuid4()}",
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
        idempotency_key=f"recovery:{uuid.uuid4()}",
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
    )

    if updated_at is not None:
        job.updated_at = updated_at

    if started_at is not None:
        job.started_at = started_at

    db.add(job)
    db.commit()
    db.refresh(job)

    return job


def test_recovery_dispatches_old_pending_job(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(minutes=5)
    job = create_job(db, status="pending", updated_at=old_time)

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        return_value=True,
    ) as dispatch:
        result = recover_investigation_jobs(db, pending_after_seconds=30)

    assert result["pending_found"] == 1
    assert result["dispatched"] == 1
    assert result["enqueue_failed"] == 0
    assert result["attempts_exhausted"] == 0
    dispatch.assert_called_once()
    assert dispatch.call_args.args[1].id == job.id


def test_recovery_resets_stale_running_job(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(hours=1)
    job = create_job(
        db,
        status="running",
        attempts=1,
        updated_at=old_time,
        started_at=old_time,
    )

    def fake_dispatch(db, recovered_job):
        assert recovered_job.status == "pending"
        assert "lease expired" in recovered_job.last_error.lower()

        recovered_job.status = "queued"
        recovered_job.cloud_task_name = "test-recovered-task"
        recovered_job.last_error = None
        db.commit()
        return True

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        side_effect=fake_dispatch,
    ) as dispatch:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=900,
        )

    assert result["stale_running_recovered"] == 1
    assert result["dispatched"] == 1
    assert result["enqueue_failed"] == 0
    dispatch.assert_called_once()

    db.refresh(job)
    assert job.status == "queued"
    assert job.cloud_task_name == "test-recovered-task"
    assert job.last_error is None
    # Recovery must not reset the attempt counter.
    assert job.attempts == 1


def test_recovery_does_not_dispatch_recent_pending_job(db_session):
    db = db_session
    job = create_job(
        db,
        status="pending",
        updated_at=datetime.now(UTC),
    )

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
    ) as dispatch:
        result = recover_investigation_jobs(db, pending_after_seconds=30)

    assert result["pending_found"] == 0
    assert result["dispatched"] == 0
    dispatch.assert_not_called()

    db.refresh(job)
    assert job.status == "pending"


def test_recovery_does_not_reclaim_active_running_job(db_session):
    """A running job whose lease is still valid must be left alone."""
    db = db_session
    job = create_job(
        db,
        status="running",
        attempts=1,
        updated_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
    )

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
    ) as dispatch:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=900,
        )

    assert result["stale_running_recovered"] == 0
    assert result["dispatched"] == 0
    dispatch.assert_not_called()

    db.refresh(job)
    assert job.status == "running"
    assert job.attempts == 1


def test_recovery_ignores_running_job_without_claim_timestamp(db_session):
    """A running job with no claim timestamp cannot be proven abandoned."""
    db = db_session
    job = create_job(
        db,
        status="running",
        attempts=1,
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        started_at=None,
    )

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
    ) as dispatch:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=900,
        )

    assert result["stale_running_recovered"] == 0
    dispatch.assert_not_called()

    db.refresh(job)
    assert job.status == "running"


def test_recovery_does_not_requeue_pending_job_at_attempt_limit(db_session):
    db = db_session
    job = create_job(
        db,
        status="pending",
        attempts=3,
        max_attempts=3,
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
    ) as dispatch:
        result = recover_investigation_jobs(db, pending_after_seconds=30)

    assert result["pending_found"] == 0
    assert result["dispatched"] == 0
    assert result["attempts_exhausted"] == 1
    dispatch.assert_not_called()

    db.refresh(job)
    assert job.status == "failed"


def test_recovery_fails_running_job_at_attempt_limit(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(hours=1)
    job = create_job(
        db,
        status="running",
        attempts=3,
        max_attempts=3,
        updated_at=old_time,
        started_at=old_time,
    )

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
    ) as dispatch:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=900,
        )

    assert result["stale_running_recovered"] == 0
    assert result["attempts_exhausted"] == 1
    dispatch.assert_not_called()

    db.refresh(job)
    assert job.status == "failed"
    assert job.attempts == 3


def test_recovery_queue_failure_keeps_job_pending(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(minutes=5)
    job = create_job(db, status="pending", updated_at=old_time)

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        return_value=False,
    ):
        result = recover_investigation_jobs(db, pending_after_seconds=30)

    assert result["pending_found"] == 1
    assert result["dispatched"] == 0
    assert result["enqueue_failed"] == 1

    db.refresh(job)
    assert job.status == "pending"


def test_recovery_duplicate_requests_do_not_dispatch_twice(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(minutes=5)
    create_job(db, status="pending", updated_at=old_time)

    def fake_dispatch(db, job):
        job.status = "queued"
        job.cloud_task_name = "deterministic-task"
        db.commit()
        return True

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        side_effect=fake_dispatch,
    ) as dispatch:
        first = recover_investigation_jobs(db, pending_after_seconds=30)
        second = recover_investigation_jobs(db, pending_after_seconds=30)

    assert first["dispatched"] == 1
    assert second["dispatched"] == 0
    assert dispatch.call_count == 1


def test_recovery_respects_batch_limit(db_session):
    db = db_session
    old_time = datetime.now(UTC) - timedelta(minutes=5)

    for _ in range(3):
        create_job(db, status="pending", updated_at=old_time)

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        return_value=True,
    ) as dispatch:
        result = recover_investigation_jobs(
            db,
            pending_after_seconds=30,
            batch_size=2,
        )

    assert result["pending_found"] == 2
    assert result["dispatched"] == 2
    assert dispatch.call_count == 2


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"pending_after_seconds": -1}, "pending_after_seconds"),
        ({"stale_running_after_seconds": 0}, "stale_running_after_seconds"),
        ({"batch_size": 0}, "batch_size"),
    ],
)
def test_recovery_rejects_invalid_arguments(
    db_session,
    kwargs,
    expected,
):
    with pytest.raises(ValueError, match=expected):
        recover_investigation_jobs(db_session, **kwargs)
