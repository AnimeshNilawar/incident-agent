"""Tests for backend log collection.

Verifies time-window, severity, service and correlation filtering,
redaction, payload/entry limits, empty results, the none/unknown source
behaviours, the generic HTTP source (via a mock transport), and
integration of log evidence into the investigation engine.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import settings
from app.schemas.investigation import InvestigationInput
from app.services.evidence import AlertPayloadEvidenceCollector, collect_evidence
from app.services.logs import (
    BackendLogRecord,
    HTTPBackendLogCollector,
    LocalInMemoryBackendLogCollector,
    NoneBackendLogCollector,
    build_backend_log_collector,
    normalize_logs_to_records,
)


@pytest.fixture
def incident() -> InvestigationInput:
    now = datetime.now(UTC)
    return InvestigationInput(
        incident_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        fingerprint="fp-1",
        title="Test Incident",
        source="grafana",
        severity="high",
        service_name="test-service",
        summary="Something happened",
        alert_metadata={},
        first_seen_at=now,
        last_seen_at=now,
    )


def test_local_collector_filters_by_time_window(incident):
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(message="too old", timestamp=now - timedelta(minutes=20)),
            BackendLogRecord(message="relevant 1", timestamp=now - timedelta(minutes=5)),
            BackendLogRecord(message="relevant 2", timestamp=now),
            BackendLogRecord(message="too new", timestamp=now + timedelta(minutes=20)),
        ]
    )

    evidence = collector.collect(incident)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.id == "backend-logs"
    assert item.status == "available"
    assert "relevant 1" in item.content
    assert "relevant 2" in item.content
    assert "too old" not in item.content
    assert "too new" not in item.content
    assert item.metadata["entry_count"] == "2"
    # Entries are chronological.
    assert item.content.index("relevant 1") < item.content.index("relevant 2")


def test_local_collector_returns_gap_when_no_logs_match(incident):
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(message="old", timestamp=now - timedelta(minutes=60)),
        ]
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert "no relevant entries" in evidence[0].description.lower()


def test_local_collector_returns_gap_with_no_logs_at_all(incident):
    evidence = LocalInMemoryBackendLogCollector().collect(incident)

    assert len(evidence) == 1
    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert evidence[0].content is None


def test_local_collector_filters_by_severity_floor(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_min_level", "error")
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(message="debug detail", level="debug", timestamp=now),
            BackendLogRecord(message="info note", level="info", timestamp=now),
            BackendLogRecord(message="a failure", level="error", timestamp=now),
            BackendLogRecord(message="odd level", level="weird", timestamp=now),
        ]
    )

    evidence = collector.collect(incident)
    content = evidence[0].content

    assert "a failure" in content
    # Unclassifiable levels are kept on purpose.
    assert "odd level" in content
    assert "debug detail" not in content
    assert "info note" not in content


def test_local_collector_filters_by_service(incident):
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(
                message="ours", service="test-service",
                timestamp=incident.last_seen_at,
            ),
            BackendLogRecord(
                message="someone else's", service="other-service",
                timestamp=incident.last_seen_at,
            ),
            BackendLogRecord(
                message="no service tag", timestamp=incident.last_seen_at,
            ),
        ]
    )

    evidence = collector.collect(incident)
    content = evidence[0].content

    assert "ours" in content
    # Untagged entries cannot be disproven; they stay.
    assert "no service tag" in content
    assert "someone else's" not in content


def test_local_collector_filters_by_correlation_ids(incident):
    correlated = incident.model_copy(
        update={"alert_metadata": {"trace_id": "trace-abc"}}
    )
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(
                message="matched trace", trace_id="trace-abc",
                timestamp=incident.last_seen_at,
            ),
            BackendLogRecord(
                message="other trace", trace_id="trace-xyz",
                timestamp=incident.last_seen_at,
            ),
            BackendLogRecord(
                message="no identifiers", timestamp=incident.last_seen_at,
            ),
        ]
    )

    evidence = collector.collect(correlated)
    content = evidence[0].content

    assert "matched trace" in content
    assert "no identifiers" in content
    assert "other trace" not in content


def test_local_collector_redacts_sensitive_tokens(incident):
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(
                message=(
                    "ERROR: password=supersecret123 token=abc123def456 "
                    "Authorization: Bearer xyz789"
                ),
                timestamp=now,
            )
        ]
    )

    evidence = collector.collect(incident)
    content = evidence[0].content

    assert "supersecret123" not in content
    assert "abc123def456" not in content
    assert "xyz789" not in content
    assert "[REDACTED]" in content


def test_local_collector_respects_max_entries(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_max_entries", 3)
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[
            BackendLogRecord(message=f"m{i}", timestamp=now + timedelta(seconds=i))
            for i in range(10)
        ]
    )

    evidence = collector.collect(incident)
    assert evidence[0].metadata["entry_count"] == "3"


def test_local_collector_truncates_by_bytes(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_max_bytes", 200)
    now = incident.last_seen_at.replace(tzinfo=UTC)
    collector = LocalInMemoryBackendLogCollector(
        logs=[BackendLogRecord(message="X" * 500, timestamp=now)]
    )

    evidence = collector.collect(incident)
    content = evidence[0].content
    assert len(content.encode("utf-8")) <= 500  # allows small suffix
    assert "[truncated by byte limit]" in content


def test_build_collector_selects_configured_source(monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_source", "none")
    assert isinstance(build_backend_log_collector(), NoneBackendLogCollector)

    monkeypatch.setattr(settings, "backend_logs_source", "local")
    assert isinstance(
        build_backend_log_collector(), LocalInMemoryBackendLogCollector
    )

    monkeypatch.setattr(settings, "backend_logs_source", "http")
    assert isinstance(build_backend_log_collector(), HTTPBackendLogCollector)


def test_normalize_logs_to_records_handles_common_fields(incident):
    now = incident.last_seen_at.isoformat()
    records = normalize_logs_to_records(
        [
            {
                "message": "hi", "level": "ERROR", "timestamp": now,
                "service": "svc", "request_id": "r1",
            },
            {"msg": "hello", "time": now},
            {},  # missing message
            "not-a-dict",
        ]
    )

    assert len(records) == 2
    assert records[0].level == "error"
    assert records[0].service == "svc"
    assert records[0].request_id == "r1"
    assert records[1].message == "hello"


# --------------------------------------------------------------------------
# HTTP source
# --------------------------------------------------------------------------


def _http_collector(handler):
    transport = httpx.MockTransport(handler)
    return HTTPBackendLogCollector(client=httpx.Client(transport=transport))


def test_http_collector_without_url_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "")
    collector = _http_collector(
        lambda request: httpx.Response(200, json=[])
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "BACKEND_LOGS_URL" in evidence[0].description


def test_http_collector_returns_normalized_entries(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")
    now = incident.last_seen_at.replace(tzinfo=UTC)
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        captured["headers"] = {
            key.lower(): value for key, value in request.headers.items()
        }
        return httpx.Response(
            200,
            json=[
                {
                    "message": "database timed out",
                    "level": "error",
                    "timestamp": (now - timedelta(minutes=2)).isoformat(),
                    "service": "test-service",
                    "trace_id": "trace-abc",
                },
                {
                    "message": "outside window",
                    "level": "error",
                    "timestamp": (now - timedelta(hours=3)).isoformat(),
                },
            ],
        )

    evidence = _http_collector(handler).collect(incident)

    item = evidence[0]
    assert item.id == "backend-logs"
    assert "database timed out" in item.content
    assert "outside window" not in item.content
    assert item.metadata["entry_count"] == "1"
    # The request is bounded by the incident window and entry limit.
    assert captured["params"]["service"] == "test-service"
    assert "start" in captured["params"] and "end" in captured["params"]
    # No credential is attached when no token is configured.
    assert "authorization" not in captured["headers"]


def test_http_collector_sends_bearer_token_but_never_stores_it(
    incident, monkeypatch
):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")
    monkeypatch.setattr(settings, "backend_logs_auth_token", "sekret-token-123")
    now = incident.last_seen_at.replace(tzinfo=UTC)
    captured_headers = {}

    def handler(request):
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json=[{"message": "boom", "level": "error", "timestamp": now.isoformat()}],
        )

    evidence = _http_collector(handler).collect(incident)

    assert captured_headers["authorization"] == "Bearer sekret-token-123"
    item = evidence[0]
    assert item.id == "backend-logs"
    serialized = item.content + str(item.metadata) + item.description
    assert "sekret-token-123" not in serialized


def test_http_collector_timeout_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")

    def handler(request):
        raise httpx.ConnectTimeout("slow")

    evidence = _http_collector(handler).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "timeout" in evidence[0].description.lower()


def test_http_collector_http_error_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")

    evidence = _http_collector(
        lambda request: httpx.Response(503, text="unavailable")
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "503" in evidence[0].description


def test_http_collector_non_json_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")

    evidence = _http_collector(
        lambda request: httpx.Response(200, text="<html>oops</html>")
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "non-JSON" in evidence[0].description


def test_http_collector_bad_shape_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")

    evidence = _http_collector(
        lambda request: httpx.Response(200, json={"unexpected": True})
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "JSON shape" in evidence[0].description


def test_http_collector_empty_result_records_gap(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")

    evidence = _http_collector(
        lambda request: httpx.Response(200, json={"entries": []})
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "no entries matching" in evidence[0].description.lower()


def test_http_collector_redacts_log_content(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_url", "https://logs.internal/query")
    now = incident.last_seen_at.replace(tzinfo=UTC)

    evidence = _http_collector(
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "message": "auth failed for api_key=abcd1234efgh5678",
                    "level": "error",
                    "timestamp": now.isoformat(),
                }
            ],
        )
    ).collect(incident)

    assert "abcd1234efgh5678" not in evidence[0].content
    assert "[REDACTED]" in evidence[0].content


# --------------------------------------------------------------------------
# Evidence integration
# --------------------------------------------------------------------------


def test_collect_evidence_includes_backend_log_gap_by_default(incident):
    # No log collector passed: the static fallback gap is recorded.
    items = collect_evidence(incident, collectors=(AlertPayloadEvidenceCollector(),))
    ids = {item.id for item in items}

    assert "gap-backend-logs" in ids
    assert {"alert-summary", "alert-metadata"} <= ids


def test_collect_evidence_does_not_duplicate_collector_gap(incident):
    collector = LocalInMemoryBackendLogCollector()

    items = collect_evidence(
        incident, collectors=(AlertPayloadEvidenceCollector(), collector)
    )
    ids = [item.id for item in items]

    assert ids.count("gap-backend-logs") == 1


def test_engine_includes_backend_logs_in_evidence():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models.incident import Incident
    from app.models.investigation_job import InvestigationJob
    from app.services.investigation_engine import run_investigation
    from tests.test_investigation_engine import FakeProvider, valid_findings

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        incident = Incident(
            fingerprint=f"fp-{uuid.uuid4()}",
            title="T",
            source="grafana",
            severity="high",
            status="received",
            service_name="svc",
            summary="s",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
        )
        db.add(incident)
        db.flush()
        job = InvestigationJob(
            incident_id=incident.id,
            idempotency_key=f"j-{uuid.uuid4()}",
            status="queued",
            attempts=0,
            max_attempts=3,
        )
        db.add(job)
        db.commit()

        now = incident.last_seen_at.replace(tzinfo=UTC)
        collector = LocalInMemoryBackendLogCollector(
            logs=[BackendLogRecord(message="log line from backend", timestamp=now)]
        )

        row = run_investigation(
            db,
            job=job,
            incident=incident,
            provider=FakeProvider(valid_findings()),
            collectors=(AlertPayloadEvidenceCollector(), collector),
        )

        evidence_ids = {item["id"] for item in row.evidence}
        assert "backend-logs" in evidence_ids
        assert "gap-backend-logs" not in evidence_ids
        assert any(
            "log line from backend" in (item.get("content") or "")
            for item in row.evidence
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
