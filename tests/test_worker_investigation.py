"""End-to-end worker tests: HTTP delivery -> claim -> engine -> report.

The engine's LLM provider is replaced with a deterministic fake, so the
full worker path (including report persistence and job completion) runs
offline against SQLite.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
from app.models.investigation_report import InvestigationReport


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
        bind=engine, autoflush=False, expire_on_commit=False
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


def create_job(SessionLocal, summary="CPU above threshold for 10 minutes"):
    with SessionLocal() as db:
        incident = Incident(
            fingerprint=f"e2e-worker-{uuid.uuid4()}",
            title="BackendHighCPU",
            source="grafana",
            severity="high",
            status="received",
            service_name="micuppa-backend-india",
            summary=summary,
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
        )
        db.add(incident)
        db.flush()

        job = InvestigationJob(
            incident_id=incident.id,
            idempotency_key=f"e2e-worker-job:{uuid.uuid4()}",
            status="queued",
            attempts=0,
            max_attempts=3,
        )
        db.add(job)
        db.commit()

        return job.id, incident.id


def fake_findings():
    return json.dumps(
        {
            "status": "completed",
            "probable_root_cause": (
                "Sustained CPU load on the backend service, cause not "
                "further identifiable from the alert alone."
            ),
            "root_cause_evidence_ids": ["alert-summary"],
            "explanation": "The alert reports sustained CPU above threshold.",
            "confidence": 0.6,
            "suggested_fixes": [
                {
                    "title": "Inspect recent backend deployments",
                    "description": (
                        "Check whether the alert started at deploy time."
                    ),
                    "evidence_ids": ["alert-summary", "alert-metadata"],
                }
            ],
            "limitations": [
                "Backend logs were not collected",
            ],
        }
    )


class FakeProvider:
    model = "fake-model"

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = 0

    def complete_json(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider(output=fake_findings())
    monkeypatch.setattr(
        "app.services.investigation_engine.build_default_provider",
        lambda: provider,
    )
    return provider


def test_worker_investigates_and_persists_report(worker_client, fake_provider):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    response = client.post(
        "/api/v1/worker/investigate",
        json={"job_id": str(job_id), "incident_id": str(incident_id)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert fake_provider.calls == 1

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 1

        report = db.scalar(
            select(InvestigationReport).where(
                InvestigationReport.job_id == job_id
            )
        )
        assert report is not None
        assert report.status == "completed"
        assert report.confidence == 0.6
        assert report.model_name == "fake-model"
        # Evidence includes the explicit missing-source gaps.
        evidence_ids = {item["id"] for item in report.evidence}
        assert "gap-backend-logs" in evidence_ids


def test_redelivery_after_completion_does_not_duplicate_report(
    worker_client, fake_provider
):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    payload = {"job_id": str(job_id), "incident_id": str(incident_id)}

    first = client.post("/api/v1/worker/investigate", json=payload)
    second = client.post("/api/v1/worker/investigate", json=payload)

    assert first.json()["status"] == "completed"
    assert second.json()["status"] == "already_completed"

    # The engine ran exactly once; the redelivery was acknowledged.
    assert fake_provider.calls == 1

    with SessionLocal() as db:
        reports = db.scalars(
            select(InvestigationReport).where(
                InvestigationReport.job_id == job_id
            )
        ).all()
        assert len(reports) == 1


def test_provider_failure_fails_job_then_retry_succeeds(
    worker_client, monkeypatch
):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)
    payload = {"job_id": str(job_id), "incident_id": str(incident_id)}

    failing = FakeProvider(error=RuntimeError("LLM request timed out"))
    monkeypatch.setattr(
        "app.services.investigation_engine.build_default_provider",
        lambda: failing,
    )

    failed = client.post("/api/v1/worker/investigate", json=payload)

    assert failed.status_code == 500
    assert failed.json()["detail"] == "Investigation failed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 1
        assert "timed out" in job.last_error
        # No report was persisted for the failed attempt.
        assert (
            db.scalar(
                select(InvestigationReport).where(
                    InvestigationReport.job_id == job_id
                )
            )
            is None
        )

    # Redelivery with a working provider succeeds and writes one report.
    working = FakeProvider(output=fake_findings())
    monkeypatch.setattr(
        "app.services.investigation_engine.build_default_provider",
        lambda: working,
    )

    retried = client.post("/api/v1/worker/investigate", json=payload)

    assert retried.status_code == 200
    assert retried.json()["status"] == "completed"

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 2

        reports = db.scalars(
            select(InvestigationReport).where(
                InvestigationReport.job_id == job_id
            )
        ).all()
        assert len(reports) == 1
        assert reports[0].confidence == 0.6


def test_unconfigured_llm_fails_job_with_explicit_error(worker_client):
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal)

    # No provider is patched in and LLM_API_KEY is empty (test env), so
    # the engine must raise the configuration error, not invent a report.
    with patch.object(settings, "llm_api_key", ""):
        response = client.post(
            "/api/v1/worker/investigate",
            json={"job_id": str(job_id), "incident_id": str(incident_id)},
        )

    assert response.status_code == 500

    with SessionLocal() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "failed"
        assert "LLM provider is not configured" in job.last_error


def test_no_summary_still_produces_honest_inconclusive_report(
    worker_client, monkeypatch
):
    """An incident with no summary and a model that admits ignorance."""
    client, SessionLocal = worker_client
    job_id, incident_id = create_job(SessionLocal, summary=None)

    inconclusive = json.dumps(
        {
            "status": "inconclusive",
            "probable_root_cause": None,
            "root_cause_evidence_ids": [],
            "explanation": (
                "The alert provides no narrative and no other evidence "
                "sources were collected."
            ),
            "confidence": 0.1,
            "suggested_fixes": [],
            "limitations": ["No logs, metrics, or code were available"],
        }
    )
    provider = FakeProvider(output=inconclusive)
    monkeypatch.setattr(
        "app.services.investigation_engine.build_default_provider",
        lambda: provider,
    )

    response = client.post(
        "/api/v1/worker/investigate",
        json={"job_id": str(job_id), "incident_id": str(incident_id)},
    )

    assert response.status_code == 200

    with SessionLocal() as db:
        report = db.scalar(
            select(InvestigationReport).where(
                InvestigationReport.job_id == job_id
            )
        )
        assert report.status == "inconclusive"
        assert report.probable_root_cause is None
        assert report.suggested_fixes == []
        # The missing alert summary is itself recorded as a gap.
        summary_item = next(
            item for item in report.evidence if item["id"] == "alert-summary"
        )
        assert summary_item["status"] == "missing"
