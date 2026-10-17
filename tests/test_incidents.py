import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client():
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
        "fingerprint": "test-event-001",
        "title": "Event creation returned HTTP 500",
        "source": "manual",
        "severity": "high",
        "service_name": "micuppa-backend-india",
        "summary": "Synthetic test incident",
    }


def test_create_incident(client):
    response = client.post(
        "/api/v1/incidents",
        json=sample_incident(),
    )

    assert response.status_code == 200
    assert response.json()["fingerprint"] == "test-event-001"
    assert response.json()["status"] == "received"


def test_duplicate_incident_is_not_duplicated(client):
    payload = sample_incident()

    first = client.post("/api/v1/incidents", json=payload)
    second = client.post("/api/v1/incidents", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    incidents = client.get("/api/v1/incidents")

    assert incidents.status_code == 200
    assert len(incidents.json()) == 1


def test_list_and_get_incident(client):
    created = client.post(
        "/api/v1/incidents",
        json=sample_incident(),
    )

    incident_id = created.json()["id"]

    listing = client.get("/api/v1/incidents")
    detail = client.get(f"/api/v1/incidents/{incident_id}")

    assert len(listing.json()) == 1
    assert detail.status_code == 200
    assert detail.json()["id"] == incident_id


def test_missing_incident_returns_404(client):
    response = client.get(
        "/api/v1/incidents/"
        "00000000-0000-0000-0000-000000000001"
    )

    assert response.status_code == 404


def test_invalid_severity_is_rejected(client):
    payload = sample_incident()
    payload["severity"] = "super-urgent"

    response = client.post("/api/v1/incidents", json=payload)

    assert response.status_code == 422