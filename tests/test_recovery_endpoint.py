import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.api.internal import verify_recovery_request
from app.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob

RECOVERY_EMAIL = "recovery-invoker@test-project.iam.gserviceaccount.com"
RECOVERY_AUDIENCE = "https://internal.example.test/api/v1/internal/recovery"


@pytest.fixture
def recovery_client(monkeypatch):
    monkeypatch.setattr(
        settings,
        "recovery_service_account_email",
        RECOVERY_EMAIL,
    )
    monkeypatch.setattr(settings, "recovery_audience", RECOVERY_AUDIENCE)

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
    app.dependency_overrides[verify_recovery_request] = lambda: None

    with TestClient(app) as client:
        yield client, SessionLocal

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def create_old_pending_job(SessionLocal) -> uuid.UUID:
    with SessionLocal() as db:
        incident = Incident(
            fingerprint=f"recovery-endpoint-{uuid.uuid4()}",
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
            idempotency_key=f"recovery-endpoint:{uuid.uuid4()}",
            status="pending",
            updated_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(job)
        db.commit()

        return job.id


def test_recovery_endpoint_requires_authentication(recovery_client):
    client, _ = recovery_client

    app.dependency_overrides.pop(verify_recovery_request, None)

    response = client.post("/api/v1/internal/recovery")

    assert response.status_code == 401


def test_recovery_endpoint_rejects_invalid_token(recovery_client):
    client, _ = recovery_client

    app.dependency_overrides.pop(verify_recovery_request, None)

    response = client.post(
        "/api/v1/internal/recovery",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )

    assert response.status_code == 401


def test_recovery_endpoint_rejects_unexpected_service_account(
    recovery_client,
    monkeypatch,
):
    client, _ = recovery_client

    app.dependency_overrides.pop(verify_recovery_request, None)

    monkeypatch.setattr(
        "app.api.internal.id_token.verify_oauth2_token",
        lambda *args, **kwargs: {
            "email": "attacker@example.test",
            "email_verified": True,
        },
    )

    response = client.post(
        "/api/v1/internal/recovery",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 403


def test_recovery_endpoint_accepts_authorized_service_account(
    recovery_client,
    monkeypatch,
):
    client, _ = recovery_client

    app.dependency_overrides.pop(verify_recovery_request, None)

    monkeypatch.setattr(
        "app.api.internal.id_token.verify_oauth2_token",
        lambda *args, **kwargs: {
            "email": RECOVERY_EMAIL,
            "email_verified": True,
        },
    )

    response = client.post(
        "/api/v1/internal/recovery",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 200


def test_recovery_endpoint_fails_closed_without_configuration(
    recovery_client,
    monkeypatch,
):
    client, _ = recovery_client

    app.dependency_overrides.pop(verify_recovery_request, None)

    monkeypatch.setattr(settings, "recovery_service_account_email", "")
    monkeypatch.setattr(settings, "recovery_audience", "")

    response = client.post(
        "/api/v1/internal/recovery",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    "query",
    [
        "batch_size=0",
        "batch_size=100000",
        "pending_after_seconds=-1",
        "stale_running_after_seconds=0",
    ],
)
def test_recovery_endpoint_rejects_invalid_batch_parameters(
    recovery_client,
    query,
):
    client, _ = recovery_client

    response = client.post(f"/api/v1/internal/recovery?{query}")

    assert response.status_code == 422


def test_recovery_endpoint_returns_structured_counters(recovery_client):
    client, _ = recovery_client

    with patch(
        "app.api.internal.recover_investigation_jobs",
        return_value={
            "pending_found": 4,
            "stale_running_recovered": 1,
            "dispatched": 5,
            "enqueue_failed": 0,
            "attempts_exhausted": 2,
        },
    ) as recover:
        response = client.post("/api/v1/internal/recovery")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "pending_found": 4,
        "stale_running_recovered": 1,
        "dispatched": 5,
        "enqueue_failed": 0,
        "attempts_exhausted": 2,
    }
    recover.assert_called_once()


def test_recovery_endpoint_runs_real_recovery(recovery_client):
    client, SessionLocal = recovery_client
    create_old_pending_job(SessionLocal)

    with patch(
        "app.services.investigation_recovery.dispatch_investigation_job",
        return_value=True,
    ) as dispatch:
        response = client.post(
            "/api/v1/internal/recovery",
            params={
                "pending_after_seconds": 30,
                "stale_running_after_seconds": 900,
                "batch_size": 10,
            },
        )

    assert response.status_code == 200

    body = response.json()
    assert body["pending_found"] == 1
    assert body["dispatched"] == 1
    assert body["enqueue_failed"] == 0
    dispatch.assert_called_once()


def test_recovery_endpoint_uses_runtime_lease_setting(
    recovery_client,
    monkeypatch,
):
    """The lease window must come from settings at request time, not import time."""
    client, _ = recovery_client

    monkeypatch.setattr(settings, "investigation_job_lease_seconds", 1234)

    with patch(
        "app.api.internal.recover_investigation_jobs",
        return_value={
            "pending_found": 0,
            "stale_running_recovered": 0,
            "dispatched": 0,
            "enqueue_failed": 0,
            "attempts_exhausted": 0,
        },
    ) as recover:
        response = client.post("/api/v1/internal/recovery")

    assert response.status_code == 200
    assert recover.call_args.kwargs["stale_running_after_seconds"] == 1234
