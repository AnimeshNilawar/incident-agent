"""PostgreSQL integration tests for concurrency, recovery, and fencing.

These validate the atomic-claim / fencing-token behaviour against real
PostgreSQL transactions (row locks, READ COMMITTED re-evaluation) that an
in-memory SQLite suite cannot reproduce. They are marked as integration
tests and are skipped unless ``POSTGRES_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.services.investigation_recovery import recover_investigation_jobs
from app.services.job_lifecycle import (
    ClaimOutcome,
    claim_job,
    complete_job,
    fail_job,
)

pytestmark = pytest.mark.postgres_integration

LEASE_SECONDS = 900


def _create_job(
    pg_session_factory,
    *,
    status: str = "pending",
    attempts: int = 0,
    max_attempts: int = 3,
    updated_at: datetime | None = None,
    started_at: datetime | None = None,
):
    with pg_session_factory() as db:
        incident = Incident(
            fingerprint=f"pg-integration-{uuid.uuid4()}",
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
            idempotency_key=f"pg-integration:{uuid.uuid4()}",
            status=status,
            attempts=attempts,
            max_attempts=max_attempts,
            updated_at=updated_at or datetime.now(UTC),
            started_at=started_at,
        )
        db.add(job)
        db.commit()

        return job.id, incident.id


def test_concurrent_claims_only_one_wins(pg_session_factory):
    job_id, incident_id = _create_job(pg_session_factory)

    barrier = threading.Barrier(2)
    outcomes = []

    def claim():
        with pg_session_factory() as db:
            barrier.wait()
            outcomes.append(
                claim_job(db, job_id=job_id, incident_id=incident_id)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        for future in futures:
            future.result()

    claimed = [o for o in outcomes if o.outcome == ClaimOutcome.CLAIMED]
    assert len(claimed) == 1
    assert all(o.outcome == ClaimOutcome.ALREADY_RUNNING for o in outcomes if o not in claimed)

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "running"
        assert job.attempts == 1


def test_stale_worker_cannot_overwrite_newer_claim(pg_session_factory):
    old = datetime.now(UTC) - timedelta(hours=2)

    # A job abandoned mid-investigation: Worker A owns it with token T1 = 1.
    job_id, _incident_id = _create_job(
        pg_session_factory,
        status="running",
        attempts=1,
        started_at=old,
        updated_at=old,
    )
    t1 = 1

    # Recovery reclaims the abandoned lease because it demonstrably expired.
    with pg_session_factory() as db:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=LEASE_SECONDS,
        )
    assert result["stale_running_recovered"] == 1

    # Worker B claims the reclaimed job and gets a new token T2.
    with pg_session_factory() as db:
        claim_b = claim_job(db, job_id=job_id, incident_id=_incident_id)
    assert claim_b.outcome == ClaimOutcome.CLAIMED
    t2 = claim_b.claim_token
    assert t2 == t1 + 1

    # Worker A and Worker B finalize concurrently with their own tokens.
    barrier = threading.Barrier(2)
    finalized: dict[str, bool] = {}

    def finalize(name, token):
        with pg_session_factory() as db:
            barrier.wait()
            finalized[name] = complete_job(
                db, job_id=job_id, claim_token=token
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(finalize, "stale", t1),
            pool.submit(finalize, "active", t2),
        ]
        for future in futures:
            future.result()

    # The stale token cannot overwrite the state owned by the newer claim.
    assert finalized["stale"] is False
    assert finalized["active"] is True

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == t2  # never reset

    # Even the failure path of the stale worker is rejected.
    with pg_session_factory() as db:
        assert (
            fail_job(db, job_id=job_id, claim_token=t1, error="stale")
            is False
        )


def test_concurrent_recovery_reclaims_at_most_once(pg_session_factory):
    old = datetime.now(UTC) - timedelta(hours=2)

    stale_id, _ = _create_job(
        pg_session_factory,
        status="running",
        attempts=1,
        started_at=old,
        updated_at=old,
    )
    pending_id, _ = _create_job(
        pg_session_factory,
        status="pending",
        updated_at=old,
    )

    seen_names: dict[str, set[str]] = {}

    def fake_enqueue(job_id, incident_id):
        name = f"deterministic-task-{job_id}"
        seen_names.setdefault(str(job_id), set()).add(name)
        return name

    barrier = threading.Barrier(2)
    results = []

    def recover():
        with pg_session_factory() as db:
            barrier.wait()
            results.append(
                recover_investigation_jobs(
                    db,
                    pending_after_seconds=30,
                    stale_running_after_seconds=LEASE_SECONDS,
                )
            )

    with patch(
        "app.services.investigation_dispatcher.enqueue_investigation",
        side_effect=fake_enqueue,
    ), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover) for _ in range(2)]
        for future in futures:
            future.result()

    # The fenced reclaim means only one run reports the stale job recovered.
    assert sum(r["stale_running_recovered"] for r in results) == 1

    with pg_session_factory() as db:
        stale = db.get(InvestigationJob, stale_id)
        pending = db.get(InvestigationJob, pending_id)

        # Neither job is left in an active/running state.
        assert stale.status != "running"
        assert stale.attempts == 1
        assert pending.status == "queued"
        assert pending.attempts == 0

    # Enqueue idempotency: exactly one deterministic task name per job.
    assert len(seen_names[str(pending_id)]) == 1
    assert len(seen_names[str(stale_id)]) == 1


def test_recovery_enqueue_failure_leaves_jobs_recoverable(pg_session_factory):
    old = datetime.now(UTC) - timedelta(hours=2)

    stale_id, _ = _create_job(
        pg_session_factory,
        status="running",
        attempts=1,
        started_at=old,
        updated_at=old,
    )
    pending_id, _ = _create_job(
        pg_session_factory,
        status="pending",
        updated_at=old,
    )

    def failing_enqueue(job_id, incident_id):
        raise RuntimeError("Cloud Tasks unavailable")

    with patch(
        "app.services.investigation_dispatcher.enqueue_investigation",
        side_effect=failing_enqueue,
    ), pg_session_factory() as db:
        result = recover_investigation_jobs(
            db,
            pending_after_seconds=30,
            stale_running_after_seconds=LEASE_SECONDS,
        )

    assert result["enqueue_failed"] == 2
    assert result["dispatched"] == 0

    with pg_session_factory() as db:
        stale = db.get(InvestigationJob, stale_id)
        pending = db.get(InvestigationJob, pending_id)

        # Jobs stay pending (recoverable) and attempts are not reset.
        assert stale.status == "pending"
        assert stale.attempts == 1
        assert pending.status == "pending"
        assert pending.attempts == 0
        assert pending.last_error == "Cloud Tasks unavailable"


def test_concurrent_claims_respect_attempt_limit(pg_session_factory):
    job_id, incident_id = _create_job(
        pg_session_factory,
        status="pending",
        attempts=3,
        max_attempts=3,
    )

    barrier = threading.Barrier(2)
    outcomes = []

    def claim():
        with pg_session_factory() as db:
            barrier.wait()
            outcomes.append(
                claim_job(db, job_id=job_id, incident_id=incident_id)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        for future in futures:
            future.result()

    # No one can claim an exhausted job, and the limit is never exceeded.
    assert all(o.outcome == ClaimOutcome.ATTEMPT_LIMIT_REACHED for o in outcomes)

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 3
        assert job.last_error == "Maximum investigation attempts reached"


def test_stale_worker_cannot_finalize_exhausted_job(pg_session_factory):
    old = datetime.now(UTC) - timedelta(hours=2)

    job_id, _incident_id = _create_job(
        pg_session_factory,
        status="running",
        attempts=3,
        max_attempts=3,
        started_at=old,
        updated_at=old,
    )

    # Recovery terminalizes an exhausted abandoned job rather than requeueing.
    with pg_session_factory() as db:
        result = recover_investigation_jobs(
            db,
            stale_running_after_seconds=LEASE_SECONDS,
        )

    assert result["stale_running_recovered"] == 0
    assert result["attempts_exhausted"] == 1

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 3

    # The original (stale) worker can no longer finalize with its token.
    with pg_session_factory() as db:
        assert complete_job(db, job_id=job_id, claim_token=3) is False
    with pg_session_factory() as db:
        assert fail_job(db, job_id=job_id, claim_token=3, error="stale") is False

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 3


def test_claim_and_recovery_race_never_exceed_attempt_limit(
    pg_session_factory,
):
    old = datetime.now(UTC) - timedelta(minutes=10)

    job_id, incident_id = _create_job(
        pg_session_factory,
        status="pending",
        attempts=2,
        max_attempts=3,
        updated_at=old,
    )

    barrier = threading.Barrier(2)

    def do_claim():
        with pg_session_factory() as db:
            barrier.wait()
            claim_job(db, job_id=job_id, incident_id=incident_id)

    def do_recover():
        with pg_session_factory() as db:
            barrier.wait()
            with patch(
                "app.services.investigation_dispatcher.enqueue_investigation",
                side_effect=RuntimeError("Cloud Tasks unavailable"),
            ):
                recover_investigation_jobs(db, pending_after_seconds=30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(do_claim),
            pool.submit(do_recover),
        ]
        for future in futures:
            future.result()

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        # The attempt counter never exceeds max_attempts under a real race,
        # and the job always remains in a valid state.
        assert job.attempts <= job.max_attempts
        assert job.status in {"pending", "queued", "running", "failed"}
