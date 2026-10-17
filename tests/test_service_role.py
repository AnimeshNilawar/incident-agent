"""Tests for the SERVICE_ROLE router split used by the deployment layout.

``all`` is the default and is what the existing test suite and local
development run, so every pre-existing endpoint stays reachable. ``api`` and
``worker`` each mount a strict subset, which is what lets the two Cloud Run
services be configured differently from one image.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.health import check_database
from app.config import SERVICE_ROLES, Settings, settings
from app.main import create_app

PUBLIC_PATHS = {"/api/v1/incidents", "/api/v1/webhooks/grafana"}
WORKER_PATHS = {"/api/v1/worker/investigate", "/api/v1/internal/recovery"}
HEALTH_PATHS = {"/health/live", "/health/ready"}


def paths_for(service_role: str) -> set[str]:
    """Route paths exposed by a role, read from its OpenAPI schema."""
    return set(create_app(service_role).openapi()["paths"])


def test_default_role_serves_every_route():
    paths = paths_for("all")

    assert PUBLIC_PATHS <= paths
    assert WORKER_PATHS <= paths
    assert HEALTH_PATHS <= paths


def test_api_role_serves_only_public_routes():
    paths = paths_for("api")

    assert PUBLIC_PATHS <= paths
    assert HEALTH_PATHS <= paths
    assert not (WORKER_PATHS & paths)


def test_worker_role_serves_only_authenticated_routes():
    paths = paths_for("worker")

    assert WORKER_PATHS <= paths
    assert HEALTH_PATHS <= paths
    assert not (PUBLIC_PATHS & paths)


def test_unknown_service_role_is_rejected():
    with pytest.raises(ValueError, match="SERVICE_ROLE"):
        create_app("something-else")


def test_settings_validate_service_role():
    with pytest.raises(ValidationError, match="SERVICE_ROLE"):
        Settings(service_role="nope")


def test_settings_normalise_service_role():
    assert Settings(service_role="  WORKER  ").service_role == "worker"
    assert Settings(service_role="API").service_role == "api"
    assert Settings().service_role == "all"


def test_service_roles_match_the_expected_deployments():
    # Guards against a typo silently widening one role's surface.
    assert SERVICE_ROLES == {"all", "api", "worker"}


@pytest.mark.parametrize("service_role", ["api", "worker", "all"])
def test_health_endpoints_exist_for_every_role(service_role):
    application = create_app(service_role)
    application.dependency_overrides[check_database] = lambda: True

    with TestClient(application) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    application.dependency_overrides.pop(check_database, None)

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "database": "ok"}


@pytest.mark.parametrize(
    "path",
    ["/api/v1/worker/investigate", "/api/v1/internal/recovery"],
)
def test_worker_routes_are_absent_from_the_api_service(path):
    # Neither the Cloud Tasks worker nor the internal recovery route is
    # mounted on the API service, so exposing the API publicly cannot also
    # expose the authenticated worker surface.
    with TestClient(create_app("api")) as client:
        response = client.post(path)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/api/v1/incidents", "/api/v1/webhooks/grafana"],
)
def test_public_routes_are_absent_from_the_worker_service(path):
    with TestClient(create_app("worker")) as client:
        response = client.get(path)

    assert response.status_code == 404


def test_api_role_keeps_the_grafana_webhook_authenticated(monkeypatch):
    """The API service must not expose the webhook without authentication."""
    monkeypatch.setattr(
        settings, "grafana_webhook_username", "grafana-agent"
    )
    monkeypatch.setattr(settings, "grafana_webhook_password", "test-secret")

    with TestClient(create_app("api")) as client:
        response = client.post("/api/v1/webhooks/grafana", json={"alerts": []})

    assert response.status_code == 401


def test_api_role_keeps_the_incident_api_authenticated(monkeypatch):
    """The API service must not expose incidents without authentication."""
    monkeypatch.setattr(
        settings, "incident_api_username", "incident-client"
    )
    monkeypatch.setattr(
        settings, "incident_api_password", "test-incident-secret"
    )

    with TestClient(create_app("api")) as client:
        response = client.get("/api/v1/incidents")

    assert response.status_code == 401


def test_api_role_fails_closed_without_incident_api_credentials(monkeypatch):
    """With no incident credentials configured, the API rejects every call."""
    monkeypatch.setattr(settings, "incident_api_username", "")
    monkeypatch.setattr(settings, "incident_api_password", "")

    with TestClient(create_app("api")) as client:
        response = client.get("/api/v1/incidents")

    assert response.status_code == 503


def test_worker_role_keeps_the_worker_authenticated():
    """The split must not weaken the worker's OIDC gate."""
    with TestClient(create_app("worker")) as client:
        # No dependency override, so the real verification runs. With no
        # Cloud Tasks configuration this fails closed at 401/503 rather than
        # reaching the handler.
        response = client.post(
            "/api/v1/worker/investigate",
            json={"job_id": "0" * 32, "incident_id": "0" * 32},
        )

    assert response.status_code in {401, 503}
