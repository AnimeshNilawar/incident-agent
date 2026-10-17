"""Authentication tests for the incident-management API.

The incident routes (``GET``/``POST /api/v1/incidents``) must reject
unauthenticated requests using an HTTP Basic credential pair that is
*separate* from the Grafana webhook credential and from the worker/recovery
OIDC identities. These tests pin the full matrix: missing, invalid, valid,
fail-closed, and credential separation.
"""

from base64 import b64encode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app

INCIDENT_API_USERNAME = "incident-client"
INCIDENT_API_PASSWORD = "test-incident-secret"
GRAFANA_USERNAME = "grafana-agent"
GRAFANA_PASSWORD = "test-secret"

UNKNOWN_INCIDENT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def api(monkeypatch):
    """Isolated app with incident credentials configured and a fresh DB."""
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
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def sample_incident():
    return {
        "fingerprint": "auth-test-001",
        "title": "Authentication test incident",
        "source": "manual",
        "severity": "high",
        "service_name": "micuppa-backend-india",
        "summary": "Synthetic authentication test incident",
    }


def valid_auth():
    return (INCIDENT_API_USERNAME, INCIDENT_API_PASSWORD)


# --- Missing credentials --------------------------------------------------


def test_post_requires_credentials(api):
    response = api.post("/api/v1/incidents", json=sample_incident())

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Basic"


def test_list_requires_credentials(api):
    response = api.get("/api/v1/incidents")

    assert response.status_code == 401


def test_detail_requires_credentials(api):
    response = api.get(f"/api/v1/incidents/{UNKNOWN_INCIDENT_ID}")

    assert response.status_code == 401


# --- Invalid credentials --------------------------------------------------


def test_invalid_password_is_rejected(api):
    response = api.get(
        "/api/v1/incidents",
        auth=(INCIDENT_API_USERNAME, "wrong-password"),
    )

    assert response.status_code == 401


def test_invalid_username_is_rejected(api):
    response = api.get(
        "/api/v1/incidents",
        auth=("someone-else", INCIDENT_API_PASSWORD),
    )

    assert response.status_code == 401


def test_error_response_does_not_echo_credentials(api):
    response = api.get(
        "/api/v1/incidents",
        auth=(INCIDENT_API_USERNAME, "wrong-password"),
    )

    assert response.status_code == 401
    assert "wrong-password" not in response.text
    assert INCIDENT_API_PASSWORD not in response.text


def test_non_ascii_credentials_are_rejected_as_invalid(api):
    # A non-ASCII credential must be a clean 401, never a 500.
    header = b64encode(
        f"{INCIDENT_API_USERNAME}:pässwörd".encode()
    ).decode("ascii")

    response = api.get(
        "/api/v1/incidents",
        headers={"Authorization": f"Basic {header}"},
    )

    assert response.status_code == 401


# --- Valid credentials ----------------------------------------------------


def test_valid_credentials_are_accepted(api):
    response = api.get("/api/v1/incidents", auth=valid_auth())

    assert response.status_code == 200
    assert response.json() == []


def test_authenticated_create_read_and_list_roundtrip(api):
    created = api.post(
        "/api/v1/incidents",
        json=sample_incident(),
        auth=valid_auth(),
    )
    assert created.status_code == 200
    incident_id = created.json()["id"]

    listing = api.get("/api/v1/incidents", auth=valid_auth())
    assert [item["id"] for item in listing.json()] == [incident_id]

    detail = api.get(
        f"/api/v1/incidents/{incident_id}",
        auth=valid_auth(),
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == incident_id


# --- Unknown incident IDs -------------------------------------------------


def test_unknown_incident_returns_404_with_credentials(api):
    response = api.get(
        f"/api/v1/incidents/{UNKNOWN_INCIDENT_ID}",
        auth=valid_auth(),
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_unknown_incident_without_credentials_is_401_not_404(api):
    # Authentication is enforced before existence, so an unknown id must not
    # reveal whether the incident exists to an unauthenticated caller.
    response = api.get(f"/api/v1/incidents/{UNKNOWN_INCIDENT_ID}")

    assert response.status_code == 401


# --- Fail-closed configuration -------------------------------------------


def test_fails_closed_when_credentials_are_not_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "incident_api_username", "")
    monkeypatch.setattr(settings, "incident_api_password", "")

    response = api.get("/api/v1/incidents", auth=valid_auth())

    assert response.status_code == 503


def test_fails_closed_when_only_one_credential_is_configured(api, monkeypatch):
    monkeypatch.setattr(settings, "incident_api_password", "")

    response = api.get(
        "/api/v1/incidents",
        auth=(INCIDENT_API_USERNAME, "anything"),
    )

    assert response.status_code == 503


# --- Credential separation (incident API vs Grafana webhook) --------------


def test_grafana_credentials_do_not_authenticate_the_incident_api(
    api, monkeypatch
):
    monkeypatch.setattr(settings, "grafana_webhook_username", GRAFANA_USERNAME)
    monkeypatch.setattr(settings, "grafana_webhook_password", GRAFANA_PASSWORD)

    response = api.get(
        "/api/v1/incidents",
        auth=(GRAFANA_USERNAME, GRAFANA_PASSWORD),
    )

    assert response.status_code == 401


def test_incident_credentials_do_not_authenticate_the_grafana_webhook(
    api, monkeypatch
):
    monkeypatch.setattr(settings, "grafana_webhook_username", GRAFANA_USERNAME)
    monkeypatch.setattr(settings, "grafana_webhook_password", GRAFANA_PASSWORD)

    response = api.post(
        "/api/v1/webhooks/grafana",
        json={"alerts": []},
        auth=valid_auth(),
    )

    assert response.status_code == 401
