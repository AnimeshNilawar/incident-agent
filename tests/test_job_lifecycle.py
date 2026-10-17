import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.job_lifecycle import (
    ClaimOutcome,
    claim_job,
    complete_job,
    fail_job,
)


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
    status: str = "pending",
    attempts: int = 0,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    incident = Incident(
        fingerprint=f"lifecycle-test-{uuid.uuid4()}",
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
        idempotency_key=f"lifecycle:{uuid.uuid4()}",
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
    )
    db.add(job)
    db.commit()

    return job.id, incident.id


def test_pending_job_can_be_claimed_once(db_session):
    db = db_session
    job_id, incident_id = create_job(db)

    first = claim_job(db, job_id=job_id, incident_id=incident_id)
    second = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert first.outcome == ClaimOutcome.CLAIMED
    assert first.claim_token == 1
    assert second.outcome == ClaimOutcome.ALREADY_RUNNING

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "running"
    assert job.attempts == 1
    assert job.started_at is not None


@pytest.mark.parametrize("status", ["pending", "queued", "failed"])
def test_claimable_statuses_can_be_claimed(db_session, status):
    db = db_session
    job_id, incident_id = create_job(db, status=status)

    result = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert result.outcome == ClaimOutcome.CLAIMED
    assert result.claim_token == 1


def test_completed_job_is_not_reclaimed(db_session):
    db = db_session
    job_id, incident_id = create_job(db, status="completed", attempts=1)

    result = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert result.outcome == ClaimOutcome.ALREADY_COMPLETED

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "completed"
    assert job.attempts == 1


def test_claim_rejects_mismatched_incident(db_session):
    db = db_session
    job_id, _ = create_job(db)

    result = claim_job(db, job_id=job_id, incident_id=uuid.uuid4())

    assert result.outcome == ClaimOutcome.NOT_FOUND

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "pending"
    assert job.attempts == 0


def test_claim_rejects_missing_job(db_session):
    result = claim_job(
        db_session,
        job_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
    )

    assert result.outcome == ClaimOutcome.NOT_FOUND


def test_claim_enforces_attempt_limit(db_session):
    db = db_session
    job_id, incident_id = create_job(db, status="pending", attempts=3)

    result = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert result.outcome == ClaimOutcome.ATTEMPT_LIMIT_REACHED

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "failed"
    assert job.attempts == 3


def test_complete_job_requires_matching_claim_token(db_session):
    db = db_session
    job_id, incident_id = create_job(db)

    claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    completed = complete_job(
        db,
        job_id=job_id,
        claim_token=claim.claim_token,
    )

    assert completed is True

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "completed"
    assert job.completed_at is not None


def test_stale_worker_cannot_complete_after_reclaim(db_session):
    db = db_session
    job_id, incident_id = create_job(db)

    stale_claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    # A recovery pass returns the abandoned job to pending, then a new
    # worker claims it, advancing the attempt token past the stale worker.
    job = db.get(InvestigationJob, job_id)
    job.status = "pending"
    job.started_at = None
    db.commit()

    fresh_claim = claim_job(db, job_id=job_id, incident_id=incident_id)
    assert fresh_claim.claim_token == stale_claim.claim_token + 1

    superseded = complete_job(
        db,
        job_id=job_id,
        claim_token=stale_claim.claim_token,
    )

    assert superseded is False

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "running"
    assert job.attempts == fresh_claim.claim_token


def test_stale_worker_cannot_fail_after_reclaim(db_session):
    db = db_session
    job_id, incident_id = create_job(db)

    stale_claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    job = db.get(InvestigationJob, job_id)
    job.status = "pending"
    job.started_at = None
    db.commit()

    fresh_claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    superseded = fail_job(
        db,
        job_id=job_id,
        claim_token=stale_claim.claim_token,
        error="stale failure",
    )

    assert superseded is False

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "running"
    assert job.attempts == fresh_claim.claim_token
    assert job.last_error is None


def test_fail_job_records_error_for_current_claim(db_session):
    db = db_session
    job_id, incident_id = create_job(db)

    claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    failed = fail_job(
        db,
        job_id=job_id,
        claim_token=claim.claim_token,
        error="boom",
    )

    assert failed is True

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.status == "failed"
    assert job.last_error == "boom"


def test_claim_sets_claim_timestamp_used_as_lease_start(db_session):
    db = db_session
    job_id, incident_id = create_job(db)
    claimed_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    result = claim_job(
        db,
        job_id=job_id,
        incident_id=incident_id,
        now=claimed_at,
    )

    assert result.outcome == ClaimOutcome.CLAIMED

    db.expire_all()
    job = db.get(InvestigationJob, job_id)
    assert job.started_at is not None
