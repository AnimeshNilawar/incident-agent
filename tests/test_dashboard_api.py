"""Offline tests for the dashboard API and its static viewer.

Pinned here: the dashboard reuses the incident API's HTTP Basic guard
(401 with challenge, fail-closed 503, no credential echo), reports
investigation status and report availability per incident, serves the
persisted report (root cause, confidence, limitations, fixes, evidence
including explicit ``missing`` gaps) exactly as stored, redacts job
error text on the way out, 404s unknown incidents after auth, and keeps
the static viewer off the worker service.

Everything runs against SQLite with no network access.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app, create_app
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.models.investigation_report import InvestigationReport
from app.schemas.investigation import (
    EvidenceItem,
    InvestigationReportData,
    SuggestedFix,
)
from app.services.investigation_reports import persist_investigation_report

INCIDENT_API_USERNAME = "dashboard-client"
INCIDENT_API_PASSWORD = "test-dashboard-secret"

UNKNOWN_INCIDENT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def dashboard(monkeypatch):
    """Isolated app with dashboard credentials and a fresh SQLite DB."""
    monkeypatch.setattr(
        settings, "incident_api_username", INCIDENT_API_USERNAME
    )
    monkeypatch.setattr(
        settings, "incident_api_password", INCIDENT_API_PASSWORD
    )

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    TestSessionLocal = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )

    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client, TestSessionLocal

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def valid_auth():
    return (INCIDENT_API_USERNAME, INCIDENT_API_PASSWORD)


# --- Fixtures for rows ----------------------------------------------------


def make_incident(
    session_factory,
    *,
    fingerprint: str,
    title: str = "Payment failures",
    severity: str = "high",
    status: str = "received",
    service: str = "payments",
    minutes_ago: int = 30,
    summary: str | None = "High 5xx rate on the payment endpoint",
):
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)

    db = session_factory()
    try:
        incident = Incident(
            fingerprint=fingerprint,
            title=title,
            source="grafana",
            severity=severity,
            status=status,
            service_name=service,
            summary=summary,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(incident)
        db.commit()
        db.refresh(incident)
        return incident
    finally:
        db.close()


def make_job(
    session_factory,
    incident: Incident,
    *,
    status: str = "completed",
    attempts: int = 1,
    last_error: str | None = None,
):
    db = session_factory()
    try:
        job = InvestigationJob(
            incident_id=incident.id,
            idempotency_key=f"job-{uuid.uuid4()}",
            status=status,
            attempts=attempts,
            max_attempts=3,
            last_error=last_error,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


SAMPLE_EVIDENCE = [
    {
        "id": "alert-summary",
        "source": "grafana_alert",
        "description": (
            "Summary of the triggering alert as received from Grafana."
        ),
        "status": "available",
        "content": "High 5xx rate on the payment endpoint",
        "observed_at": "2026-10-18T10:00:00+00:00",
        "metadata": {},
    },
    {
        "id": "backend-logs",
        "source": "backend_logs",
        "description": (
            "2 backend log entries collected between 2026-10-18T09:50:00"
            "+00:00 and 2026-10-18T10:10:00+00:00."
        ),
        "status": "available",
        "content": (
            "2026-10-18T10:01:00+00:00 [ERROR] (payments) "
            "payment gateway timeout\n"
            "2026-10-18T10:02:00+00:00 [ERROR] (payments) "
            "retry failed api_key=[REDACTED]"
        ),
        "observed_at": "2026-10-18T10:02:00+00:00",
        "metadata": {"origin": "local:in-memory", "entry_count": "2"},
    },
    {
        "id": "gap-posthog-errors",
        "source": "frontend_errors",
        "description": (
            "Frontend error collection from PostHog is not configured."
        ),
        "status": "missing",
        "content": None,
        "observed_at": None,
        "metadata": {},
    },
]


def make_report(
    session_factory,
    incident: Incident,
    job: InvestigationJob,
    *,
    status: str = "completed",
):
    db = session_factory()
    try:
        report = InvestigationReport(
            job_id=job.id,
            incident_id=incident.id,
            status=status,
            probable_root_cause=(
                "Deployment 42 introduced a null dereference in the "
                "payment retry path"
                if status == "completed"
                else None
            ),
            root_cause_evidence_ids=["backend-logs"],
            explanation=(
                "Logs show payment gateway timeouts starting at 10:01, "
                "matching the deployment time."
            ),
            confidence=0.72,
            suggested_fixes=[
                {
                    "title": "Roll back deployment 42",
                    "description": "Revert the payment service to release 41.",
                    "evidence_ids": ["backend-logs"],
                }
            ],
            evidence=SAMPLE_EVIDENCE,
            limitations=[
                "Frontend errors were not collected (PostHog unconfigured)."
            ],
            model_name="gpt-4o-mini",
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        return report
    finally:
        db.close()


# --- Authentication -------------------------------------------------------


def test_list_requires_credentials(dashboard):
    client, _session = dashboard

    response = client.get("/api/v1/dashboard/incidents")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Basic"


def test_detail_requires_credentials(dashboard):
    client, _session = dashboard

    response = client.get(f"/api/v1/dashboard/incidents/{UNKNOWN_INCIDENT_ID}")

    assert response.status_code == 401


def test_unknown_incident_without_credentials_is_401_not_404(dashboard):
    # Auth is enforced before existence: unauthenticated callers cannot
    # probe which incident IDs exist.
    client, _session = dashboard

    response = client.get(f"/api/v1/dashboard/incidents/{UNKNOWN_INCIDENT_ID}")

    assert response.status_code == 401


def test_invalid_credentials_are_rejected_without_echo(dashboard):
    client, _session = dashboard

    response = client.get(
        "/api/v1/dashboard/incidents",
        auth=(INCIDENT_API_USERNAME, "wrong-password"),
    )

    assert response.status_code == 401
    assert "wrong-password" not in response.text
    assert INCIDENT_API_PASSWORD not in response.text


def test_fails_closed_without_configured_credentials(dashboard, monkeypatch):
    client, _session = dashboard
    monkeypatch.setattr(settings, "incident_api_username", "")
    monkeypatch.setattr(settings, "incident_api_password", "")

    response = client.get(
        "/api/v1/dashboard/incidents",
        auth=valid_auth(),
    )

    assert response.status_code == 503


# --- List endpoint ----------------------------------------------------------


def test_list_orders_newest_first_and_reports_investigation_state(
    dashboard,
):
    client, session_factory = dashboard

    make_incident(
        session_factory, fingerprint="fp-old", minutes_ago=120
    )
    newest = make_incident(
        session_factory, fingerprint="fp-new", minutes_ago=5
    )
    middle = make_incident(
        session_factory,
        fingerprint="fp-mid",
        title="Search outage",
        minutes_ago=60,
    )

    # newest: completed job + report; middle: queued job, no report;
    # older: no investigation at all.
    job = make_job(session_factory, newest, status="completed")
    make_report(session_factory, newest, job)
    make_job(session_factory, middle, status="queued")

    response = client.get("/api/v1/dashboard/incidents", auth=valid_auth())

    assert response.status_code == 200
    rows = response.json()

    assert [row["fingerprint"] for row in rows] == [
        "fp-new",
        "fp-mid",
        "fp-old",
    ]

    by_fingerprint = {row["fingerprint"]: row for row in rows}

    investigated = by_fingerprint["fp-new"]["investigation"]
    assert investigated["job_status"] == "completed"
    assert investigated["report_available"] is True
    assert investigated["report_status"] == "completed"

    queued = by_fingerprint["fp-mid"]["investigation"]
    assert queued["job_status"] == "queued"
    assert queued["report_available"] is False
    assert queued["report_status"] is None

    untouched = by_fingerprint["fp-old"]["investigation"]
    assert untouched["job_id"] is None
    assert untouched["job_status"] is None
    assert untouched["report_available"] is False

    # The list carries the incident fields the UI needs in one read.
    assert rows[0]["title"] == "Payment failures"
    assert rows[0]["service_name"] == "payments"
    assert rows[0]["severity"] == "high"
    assert rows[0]["status"] == "received"
    assert rows[0]["first_seen_at"]
    assert rows[0]["last_seen_at"]


def test_list_respects_paging_parameters(dashboard):
    client, session_factory = dashboard

    for index in range(3):
        make_incident(session_factory, fingerprint=f"fp-{index}")

    page = client.get(
        "/api/v1/dashboard/incidents?limit=2",
        auth=valid_auth(),
    )
    offset_page = client.get(
        "/api/v1/dashboard/incidents?limit=2&offset=2",
        auth=valid_auth(),
    )

    assert len(page.json()) == 2
    assert len(offset_page.json()) == 1


# --- Detail endpoint ---------------------------------------------------------


def test_detail_returns_report_with_evidence_fixes_and_gaps(dashboard):
    client, session_factory = dashboard

    incident = make_incident(session_factory, fingerprint="fp-detail")
    job = make_job(session_factory, incident, status="completed")
    make_report(session_factory, incident, job)

    response = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    )

    assert response.status_code == 200
    payload = response.json()

    # Incident fields for the detail header.
    assert payload["id"] == str(incident.id)
    assert payload["fingerprint"] == "fp-detail"
    assert payload["summary"] == "High 5xx rate on the payment endpoint"

    # Investigation job and info.
    assert payload["investigation"]["job_status"] == "completed"
    assert payload["investigation"]["report_available"] is True
    assert payload["job"]["status"] == "completed"
    assert payload["job"]["attempts"] == 1
    assert payload["job"]["max_attempts"] == 3

    report = payload["report"]
    assert report["status"] == "completed"
    assert report["probable_root_cause"].startswith("Deployment 42")
    assert report["root_cause_evidence_ids"] == ["backend-logs"]
    assert report["confidence"] == pytest.approx(0.72)
    assert report["model_name"] == "gpt-4o-mini"
    assert report["limitations"] == [
        "Frontend errors were not collected (PostHog unconfigured)."
    ]

    fixes = report["suggested_fixes"]
    assert fixes[0]["title"] == "Roll back deployment 42"
    assert fixes[0]["evidence_ids"] == ["backend-logs"]

    # Evidence arrives exactly as stored: available items with content
    # and metadata, plus the explicit missing gap.
    statuses = {item["id"]: item["status"] for item in report["evidence"]}
    assert statuses == {
        "alert-summary": "available",
        "backend-logs": "available",
        "gap-posthog-errors": "missing",
    }

    by_id = {item["id"]: item for item in report["evidence"]}
    assert by_id["backend-logs"]["content"] is not None
    assert by_id["backend-logs"]["metadata"]["entry_count"] == "2"
    assert by_id["gap-posthog-errors"]["content"] is None
    assert by_id["gap-posthog-errors"]["source"] == "frontend_errors"


def test_detail_of_inconclusive_report_has_no_root_cause(dashboard):
    client, session_factory = dashboard

    incident = make_incident(session_factory, fingerprint="fp-inconc")
    job = make_job(session_factory, incident, status="completed")
    make_report(session_factory, incident, job, status="inconclusive")

    report = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    ).json()["report"]

    assert report["status"] == "inconclusive"
    assert report["probable_root_cause"] is None


def test_detail_404s_unknown_incident_after_authentication(dashboard):
    client, _session = dashboard

    response = client.get(
        f"/api/v1/dashboard/incidents/{UNKNOWN_INCIDENT_ID}",
        auth=valid_auth(),
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_detail_without_job_or_report_is_explicitly_empty(dashboard):
    client, session_factory = dashboard

    incident = make_incident(session_factory, fingerprint="fp-bare")

    payload = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    ).json()

    assert payload["job"] is None
    assert payload["report"] is None
    assert payload["investigation"]["job_status"] is None
    assert payload["investigation"]["report_available"] is False


def test_detail_redacts_job_last_error(dashboard):
    client, session_factory = dashboard

    incident = make_incident(session_factory, fingerprint="fp-failed")
    make_job(
        session_factory,
        incident,
        status="failed",
        attempts=3,
        last_error=(
            "LLM call failed: connection refused "
            "(api_key=sk-super-secret-value)"
        ),
    )

    response = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["status"] == "failed"
    assert "[REDACTED]" in payload["job"]["last_error"]
    assert "sk-super-secret-value" not in response.text


def test_responses_never_contain_configured_secrets(
    dashboard, monkeypatch
):
    client, session_factory = dashboard
    monkeypatch.setattr(settings, "grafana_webhook_password", "grafana-pw")

    incident = make_incident(session_factory, fingerprint="fp-secrets")
    job = make_job(session_factory, incident)
    make_report(session_factory, incident, job)

    listing = client.get("/api/v1/dashboard/incidents", auth=valid_auth())
    detail = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    )

    for response in (listing, detail):
        assert response.status_code == 200
        assert INCIDENT_API_PASSWORD not in response.text
        assert "grafana-pw" not in response.text


def test_report_written_by_the_persist_service_is_served(dashboard):
    # End-to-end consistency: a report persisted exactly the way the
    # engine persists it (typed contracts -> JSON columns) reads back
    # through the dashboard API without any shape drift.
    client, session_factory = dashboard

    incident = make_incident(session_factory, fingerprint="fp-engine")
    job = make_job(session_factory, incident, status="completed")

    report_data = InvestigationReportData(
        job_id=job.id,
        incident_id=incident.id,
        status="completed",
        probable_root_cause="Connection pool exhaustion after release 41",
        root_cause_evidence_ids=["backend-logs"],
        explanation="Backend logs show pool wait timeouts.",
        confidence=0.9,
        suggested_fixes=[
            SuggestedFix(
                title="Increase the connection pool size",
                description="Raise max connections to 40.",
                evidence_ids=["backend-logs"],
            )
        ],
        evidence=[EvidenceItem(**item) for item in SAMPLE_EVIDENCE],
        limitations=["Frontend errors were not collected."],
        model_name="gpt-4o-mini",
    )

    db = session_factory()
    try:
        persist_investigation_report(db, report_data)
    finally:
        db.close()

    report = client.get(
        f"/api/v1/dashboard/incidents/{incident.id}",
        auth=valid_auth(),
    ).json()["report"]

    assert report["status"] == "completed"
    assert report["probable_root_cause"].startswith("Connection pool")
    assert report["confidence"] == pytest.approx(0.9)
    assert [item["id"] for item in report["evidence"]] == [
        "alert-summary",
        "backend-logs",
        "gap-posthog-errors",
    ]
    assert report["suggested_fixes"][0]["title"] == (
        "Increase the connection pool size"
    )


# --- Static viewer -----------------------------------------------------------


def test_dashboard_index_is_served_without_credentials(dashboard):
    client, _session = dashboard

    response = client.get("/dashboard/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="view-list"' in response.text
    assert 'id="view-detail"' in response.text


def test_dashboard_assets_are_served(dashboard):
    client, _session = dashboard

    script = client.get("/dashboard/app.js")
    stylesheet = client.get("/dashboard/app.css")

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]


def test_bare_dashboard_path_resolves_to_the_viewer(dashboard):
    client, _session = dashboard

    response = client.get("/dashboard")

    assert response.status_code in {200, 307}


def test_static_assets_contain_no_credentials(dashboard):
    client, _session = dashboard

    for asset in ("/dashboard/", "/dashboard/app.js", "/dashboard/app.css"):
        response = client.get(asset)
        assert INCIDENT_API_PASSWORD not in response.text
        assert INCIDENT_API_USERNAME not in response.text


# --- Service-role surface -----------------------------------------------------


def test_dashboard_routes_follow_service_roles():
    for role in ("all", "api"):
        paths = set(create_app(role).openapi()["paths"])
        assert "/api/v1/dashboard/incidents" in paths

    worker_paths = set(create_app("worker").openapi()["paths"])
    assert "/api/v1/dashboard/incidents" not in worker_paths


def test_api_role_serves_the_dashboard_viewer():
    with TestClient(create_app("api")) as client:
        response = client.get("/dashboard/")

    assert response.status_code == 200


def test_worker_role_has_no_dashboard_viewer():
    with TestClient(create_app("worker")) as client:
        response = client.get("/dashboard/")

    assert response.status_code == 404
