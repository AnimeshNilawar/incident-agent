
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client(monkeypatch):
    # Configure test-only webhook credentials.
    monkeypatch.setattr(settings, "grafana_webhook_username", "grafana-agent")
    monkeypatch.setattr(settings, "grafana_webhook_password", "test-secret")

    # The Grafana webhook writes incidents directly through the service layer,
    # but the tests read them back over the (now authenticated) incident API.
    monkeypatch.setattr(
        settings, "incident_api_username", "incident-client"
    )
    monkeypatch.setattr(
        settings, "incident_api_password", "test-incident-secret"
    )

    # Keep test data isolated from the local application database.
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


@pytest.fixture
def firing_payload():
    return {
        "receiver": "micuppa-incident-agent",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "grafana-test-fingerprint-001",
                "labels": {
                    "alertname": "BackendHighCPU",
                    "severity": "critical",
                    "service_name": "micuppa-backend-india",
                },
                "annotations": {
                    "summary": "Backend CPU usage is high",
                    "description": "CPU usage exceeded the configured threshold.",
                },
            }
        ],
    }


def send_webhook(client, payload, credentials=("grafana-agent", "test-secret")):
    return client.post(
        "/api/v1/webhooks/grafana",
        json=payload,
        auth=credentials,
    )


def get_incidents(client):
    response = client.get(
        "/api/v1/incidents",
        auth=("incident-client", "test-incident-secret"),
    )
    assert response.status_code == 200
    return response.json()


def test_webhook_requires_authentication(client, firing_payload):
    response = client.post(
        "/api/v1/webhooks/grafana",
        json=firing_payload,
    )

    assert response.status_code == 401


def test_webhook_rejects_invalid_credentials(client, firing_payload):
    response = send_webhook(
        client,
        firing_payload,
        credentials=("grafana-agent", "wrong-password"),
    )

    assert response.status_code == 401


def test_webhook_fails_closed_when_credentials_are_not_configured(
    client,
    firing_payload,
    monkeypatch,
):
    # Deployment guard: if the operator has not configured the webhook
    # credentials, the endpoint must refuse every request (503) rather than
    # accept an unauthenticated payload.
    monkeypatch.setattr(settings, "grafana_webhook_username", "")
    monkeypatch.setattr(settings, "grafana_webhook_password", "")

    response = send_webhook(client, firing_payload)

    assert response.status_code == 503


def test_webhook_error_responses_do_not_echo_credentials(
    client,
    firing_payload,
):
    response = send_webhook(
        client,
        firing_payload,
        credentials=("grafana-agent", "wrong-password"),
    )

    assert response.status_code == 401
    assert "wrong-password" not in response.text
    assert "test-secret" not in response.text


def test_firing_alert_creates_incident(client, firing_payload):
    response = send_webhook(client, firing_payload)

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "received": 1,
        "created": 1,
        "updated": 0,
        "resolved": 0,
    }

    incidents = get_incidents(client)

    assert len(incidents) == 1
    assert incidents[0]["fingerprint"] == (
        "grafana:grafana-test-fingerprint-001"
    )
    assert incidents[0]["title"] == "BackendHighCPU"
    assert incidents[0]["severity"] == "critical"
    assert incidents[0]["service_name"] == "micuppa-backend-india"


def test_repeated_firing_alert_does_not_duplicate_incident(
    client,
    firing_payload,
):
    first_response = send_webhook(client, firing_payload)
    second_response = send_webhook(client, firing_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    assert second_response.json()["created"] == 0
    assert second_response.json()["updated"] == 1

    incidents = get_incidents(client)
    assert len(incidents) == 1


def test_resolved_alert_completes_incident(client, firing_payload):
    send_webhook(client, firing_payload)

    resolved_payload = {
        **firing_payload,
        "status": "resolved",
        "alerts": [
            {
                **firing_payload["alerts"][0],
                "status": "resolved",
            }
        ],
    }

    response = send_webhook(client, resolved_payload)

    assert response.status_code == 200
    assert response.json()["resolved"] == 1

    incidents = get_incidents(client)

    assert len(incidents) == 1
    assert incidents[0]["status"] == "completed"


def test_firing_alert_reopens_completed_incident(client, firing_payload):
    send_webhook(client, firing_payload)

    resolved_payload = {
        **firing_payload,
        "status": "resolved",
        "alerts": [
            {
                **firing_payload["alerts"][0],
                "status": "resolved",
            }
        ],
    }

    send_webhook(client, resolved_payload)
    response = send_webhook(client, firing_payload)

    assert response.status_code == 200
    assert response.json()["updated"] == 1

    incidents = get_incidents(client)

    assert len(incidents) == 1
    assert incidents[0]["status"] == "received"


def test_webhook_rejects_invalid_alert_entries(client, firing_payload):
    invalid_payload = {
        **firing_payload,
        "alerts": ["not-an-alert-object"],
    }

    response = send_webhook(client, invalid_payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Each alert must be an object"


def test_firing_alert_attempts_to_dispatch_investigation_job(
    client,
    firing_payload,
):
    with patch(
        "app.api.grafana.dispatch_investigation_job",
        return_value=True,
    ) as dispatch:
        response = send_webhook(client, firing_payload)

    assert response.status_code == 200
    assert response.json()["created"] == 1

    dispatch.assert_called_once()

    _db, job = dispatch.call_args.args

    assert job.status == "pending"
    assert job.incident_id is not None
