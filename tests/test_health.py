from fastapi.testclient import TestClient

from app.api.health import check_database
from app.main import app

client = TestClient(app)


def test_health_live():
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_ready_reports_ready_when_database_is_reachable():
    app.dependency_overrides[check_database] = lambda: True
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(check_database, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_health_ready_reports_unavailable_when_database_is_down():
    app.dependency_overrides[check_database] = lambda: False
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(check_database, None)

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unreachable",
    }


def test_health_endpoints_never_expose_connection_details():
    app.dependency_overrides[check_database] = lambda: False
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(check_database, None)

    body = response.text.lower()

    assert "sqlite" not in body
    assert "postgres" not in body
    assert "password" not in body
    assert "traceback" not in body
    assert "/" not in response.json()["database"]


def test_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["service"] == "incident-agent"
