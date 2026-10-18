
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.api.worker import verify_cloud_tasks_request
from app.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob


@pytest.fixture
def worker_client(monkeypatch):
    monkeypatch.setattr(
        settings,
        "cloud_tasks_service_account_email",
        "tasks-invoker@test-project.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        settings,
        "investigation_worker_url",
        "https://worker.example.test/api/v1/worker/investigate",
    )
    monkeypatch.setattr(settings, "investigation_worker_audience", "")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_cloud_tasks_request] = lambda: None

    with TestClient(app) as client:
        yield client, SessionLocal

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def create_job(
    SessionLocal,
    status="queued",
    attempts=0,
    max_attempts=3,
    started_at=None,
):
    with SessionLocal() as db:
        incident = Incident(
            fingerprint=f"worker-test-{uuid.uuid4()}",
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
            idempotency_key=f"worker-job:{uuid.uuid4()}",
            status=status,
            attempts=attempts,
            max_attempts=max_attempts,
        )

        if started_at is not None:
            job.started_at = started_at

        db.add(job)
        db.commit()

        return job.id, incident.id


def test_worker_completes_queued_job(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    # Lifecycle-only test: the engine itself is covered in
    # tests/test_investigation_engine.py and needs a configured provider.
    with patch("app.api.worker.execute_investigation"):
        response = client.post(
            "/api/v1/worker/investigate",
            json={
                "job_id": str(job_id),
                "incident_id": str(incident_id),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 1
        assert job.completed_at is not None


def test_worker_acknowledges_already_completed_job(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(
        SessionLocal,
        status="completed",
        attempts=1,
    )

    response = client.post(
        "/api/v1/worker/investigate",
        json={
            "job_id": str(job_id),
            "incident_id": str(incident_id),
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "already_completed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.attempts == 1


def test_worker_rejects_mismatched_incident(worker_client):
    client, SessionLocal = worker_client
    job_id, _ = create_job(SessionLocal)

    response = client.post(
        "/api/v1/worker/investigate",
        json={
            "job_id": str(job_id),
            "incident_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 404


def test_worker_rejects_missing_authentication(worker_client):
    client, _ = worker_client

    app.dependency_overrides.pop(verify_cloud_tasks_request)

    response = client.post(
        "/api/v1/worker/investigate",
        json={
            "job_id": str(uuid.uuid4()),
            "incident_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 401


def test_worker_does_not_run_investigation_twice(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    payload = {
        "job_id": str(job_id),
        "incident_id": str(incident_id),
    }

    with patch("app.api.worker.execute_investigation") as investigate:
        first = client.post("/api/v1/worker/investigate", json=payload)
        second = client.post("/api/v1/worker/investigate", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert second.status_code == 200
    assert second.json()["status"] == "already_completed"

    investigate.assert_called_once()

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 1


def test_worker_rejects_active_running_job(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(
        SessionLocal,
        status="running",
        attempts=1,
        started_at=datetime.now(UTC),
    )

    response = client.post(
        "/api/v1/worker/investigate",
        json={
            "job_id": str(job_id),
            "incident_id": str(incident_id),
        },
    )

    assert response.status_code == 409

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "running"
        assert job.attempts == 1


def test_worker_records_transient_failure_and_allows_retry(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    payload = {
        "job_id": str(job_id),
        "incident_id": str(incident_id),
    }

    with patch(
        "app.api.worker.execute_investigation",
        side_effect=RuntimeError("temporary downstream outage"),
    ):
        failed = client.post("/api/v1/worker/investigate", json=payload)

    assert failed.status_code == 500
    assert failed.json()["detail"] == "Investigation failed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 1
        assert job.last_error == "temporary downstream outage"

    # Cloud Tasks redelivery can retry a transiently failed job.
    with patch("app.api.worker.execute_investigation"):
        retried = client.post("/api/v1/worker/investigate", json=payload)

    assert retried.status_code == 200
    assert retried.json()["status"] == "completed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 2


def test_worker_enforces_attempt_limit(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(
        SessionLocal,
        status="pending",
        attempts=3,
        max_attempts=3,
    )

    with patch("app.api.worker.execute_investigation") as investigate:
        response = client.post(
            "/api/v1/worker/investigate",
            json={
                "job_id": str(job_id),
                "incident_id": str(incident_id),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "attempt_limit_reached"
    investigate.assert_not_called()

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 3


def test_worker_reports_superseded_completion(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    with (
        patch("app.api.worker.execute_investigation"),
        patch("app.api.worker.complete_job", return_value=False) as complete,
    ):
        response = client.post(
            "/api/v1/worker/investigate",
            json={
                "job_id": str(job_id),
                "incident_id": str(incident_id),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "superseded"
    complete.assert_called_once()

