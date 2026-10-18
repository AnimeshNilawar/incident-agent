"""Tests for the Google Cloud Logging collector.

Everything runs offline: Application Default Credentials are replaced by
an injected fake credential provider and the HTTP call goes through an
``httpx.MockTransport``. No request ever leaves the process and the real
Cloud Logging API is never contacted. Covered: factory registration,
query/filter construction (resource, service, window, severity floor),
entry normalization (text/json/proto payloads, trace extraction),
client-side re-filtering, service mapping, redaction, the access-token
handling, and every explicit gap path (missing project, missing ADC,
refresh failure, missing token, permission denial, timeout, server
error, malformed responses, empty results).
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from google.auth.exceptions import DefaultCredentialsError, RefreshError

from app.config import settings
from app.schemas.investigation import InvestigationInput
from app.services.gcp_logging import (
    ENTRIES_URL,
    GoogleCloudLoggingCollector,
)
from app.services.logs import (
    LocalInMemoryBackendLogCollector,
    build_backend_log_collector,
)

PROJECT = "proj-123"
TOKEN = "fake-gcp-access-token"


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


@pytest.fixture(autouse=True)
def gcp_project(monkeypatch):
    monkeypatch.setattr(settings, "gcp_project_id", PROJECT)


class FakeCredentials:
    """Minimal stand-in for google.auth credentials (no network)."""

    def __init__(self, token=TOKEN, refresh_error=None):
        self.token = token
        self.refresh_error = refresh_error
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error


def _make_collector(handler, credentials=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleCloudLoggingCollector(
        client=client,
        credentials_provider=Mock(
            return_value=credentials or FakeCredentials()
        ),
    )


def _timestamp(incident, minutes=0):
    moment = incident.last_seen_at.replace(tzinfo=UTC) + timedelta(
        minutes=minutes
    )
    return moment.isoformat()


def _entry(message, incident, *, minutes=0, severity="ERROR", **extra):
    entry = {
        "logName": f"projects/{PROJECT}/logs/micuppa-api",
        "resource": {
            "type": "cloud_run_revision",
            "labels": {"service_name": "test-service"},
        },
        "severity": severity,
        "timestamp": _timestamp(incident, minutes=minutes),
        "textPayload": message,
    }
    entry.update(extra)
    return entry


def _ok_handler(entries, captured=None):
    def handler(request):
        if captured is not None:
            captured["request"] = request
        return httpx.Response(200, json={"entries": entries})

    return handler


def _no_request_handler(request):
    pytest.fail("no HTTP request should be made in this test")


# ---------------------------------------------------------------------------
# Registration and configuration
# ---------------------------------------------------------------------------


def test_factory_returns_gcp_collector_for_gcp_source(monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_source", "gcp")

    collector = build_backend_log_collector()

    assert isinstance(collector, GoogleCloudLoggingCollector)


def test_default_source_stays_local_and_offline():
    collector = build_backend_log_collector()

    assert isinstance(collector, LocalInMemoryBackendLogCollector)


def test_missing_project_records_gap_without_any_request(
    incident, monkeypatch
):
    monkeypatch.setattr(settings, "gcp_project_id", "")
    collector = _make_collector(_no_request_handler)

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert "GCP_PROJECT_ID" in evidence[0].description


def test_missing_service_name_records_gap_without_any_request(incident):
    collector = _make_collector(_no_request_handler)
    incident = incident.model_copy(update={"service_name": ""})

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert "service" in evidence[0].description.lower()


# ---------------------------------------------------------------------------
# Successful collection
# ---------------------------------------------------------------------------


def test_collects_and_normalizes_entries(incident):
    captured = {}
    entries = [
        _entry("database connection pool exhausted", incident, minutes=-5),
        {
            "logName": f"projects/{PROJECT}/logs/micuppa-api",
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"service_name": "test-service"},
            },
            "severity": "WARNING",
            "timestamp": _timestamp(incident, minutes=-3),
            "jsonPayload": {"message": "slow query detected"},
            "trace": f"projects/{PROJECT}/traces/abc123",
            "spanId": "span-1",
        },
    ]
    collector = _make_collector(_ok_handler(entries, captured))
    credentials = collector._credentials_provider.return_value

    evidence = collector.collect(incident)

    assert credentials.refresh_calls == 1
    request = captured["request"]
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == ENTRIES_URL
    assert request.url.host == "logging.googleapis.com"
    assert request.url.params.get_list("resourceNames") == [
        f"projects/{PROJECT}"
    ]
    assert request.url.params["orderBy"] == "timestamp asc"
    assert request.url.params["pageSize"] == str(
        settings.backend_logs_max_entries
    )
    assert request.headers["authorization"] == f"Bearer {TOKEN}"

    query_filter = request.url.params["filter"]
    assert 'resource.type="cloud_run_revision"' in query_filter
    assert 'resource.labels.service_name="test-service"' in query_filter
    assert 'timestamp>="' in query_filter
    window_start = incident.last_seen_at.replace(tzinfo=UTC) - timedelta(
        minutes=settings.backend_logs_window_minutes_before
    )
    expected_start = window_start.isoformat().replace("+00:00", "Z")
    assert f'timestamp>="{expected_start}"' in query_filter

    assert len(evidence) == 1
    item = evidence[0]
    assert item.id == "backend-logs"
    assert item.status == "available"
    assert "database connection pool exhausted" in item.content
    assert "slow query detected" in item.content
    assert "[ERROR]" in item.content
    assert "[WARNING]" in item.content
    # The record carries the incident's service name.
    assert "(test-service)" in item.content
    # The full trace resource name is reduced to the bare trace ID.
    assert "trace=abc123" in item.content
    assert "span-1" in str(item.metadata)
    assert "projects/proj-123/traces" not in item.content
    assert item.metadata["entry_count"] == "2"
    assert item.metadata["origin"] == (
        f"gcp:projects/{PROJECT}/services/test-service"
    )
    # Chronological order.
    assert (
        item.content.index("database connection pool exhausted")
        < item.content.index("slow query detected")
    )


def test_request_id_is_taken_from_json_payload(incident):
    entries = [
        _entry(
            "handled request",
            incident,
            jsonPayload={"message": "handled request", "request_id": "req-9"},
        )
    ]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(incident)[0]

    assert "req=req-9" in item.content


def test_proto_payload_is_summarized(incident):
    entries = [
        _entry(
            "",
            incident,
            protoPayload={
                "methodName": "google.cloud.run.v2.Services.UpdateService",
                "status": {"message": "quota exceeded"},
            },
        )
    ]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(incident)[0]

    assert "UpdateService" in item.content


def test_redaction_applies_to_gcp_entries(incident):
    entries = [
        _entry("connecting with api_key=super-secret-value now", incident)
    ]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(incident)[0]

    assert "[REDACTED]" in item.content
    assert "super-secret-value" not in item.content


def test_access_token_is_never_stored_in_evidence(incident):
    entries = [_entry("a logged line", incident)]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(incident)[0]

    blob = (
        item.description
        + (item.content or "")
        + " ".join(item.metadata.values())
    )
    assert TOKEN not in blob


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_severity_floor_is_pushed_down_and_reapplied(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_min_level", "error")
    captured = {}
    entries = [
        _entry("debug detail", incident, severity="DEBUG"),
        _entry("info note", incident, severity="INFO"),
        _entry("a failure", incident, severity="ERROR"),
    ]
    collector = _make_collector(_ok_handler(entries, captured))

    item = collector.collect(incident)[0]

    assert "severity>=ERROR" in captured["request"].url.params["filter"]
    assert "a failure" in item.content
    assert "debug detail" not in item.content
    assert "info note" not in item.content


def test_alert_severity_is_treated_as_critical(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_min_level", "critical")
    entries = [
        _entry("page-worthy", incident, severity="ALERT"),
        _entry("merely bad", incident, severity="WARNING"),
    ]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(incident)[0]

    assert "page-worthy" in item.content
    assert "merely bad" not in item.content


def test_correlation_ids_filter_entries_client_side(incident):
    correlated = incident.model_copy(
        update={"alert_metadata": {"trace_id": "trace-abc"}}
    )
    entries = [
        _entry(
            "matches the alert",
            correlated,
            trace=f"projects/{PROJECT}/traces/trace-abc",
        ),
        _entry(
            "different trace",
            correlated,
            trace=f"projects/{PROJECT}/traces/other-trace",
        ),
        _entry("no identifiers at all", correlated),
    ]
    collector = _make_collector(_ok_handler(entries))

    item = collector.collect(correlated)[0]

    assert "matches the alert" in item.content
    # Lines without identifiers cannot be disproven; they stay.
    assert "no identifiers at all" in item.content
    assert "different trace" not in item.content


def test_entries_outside_the_window_record_a_gap(incident):
    entries = [_entry("long before the incident", incident, minutes=-60)]
    collector = _make_collector(_ok_handler(entries))

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"


# ---------------------------------------------------------------------------
# Service resolution
# ---------------------------------------------------------------------------


def test_service_map_renames_the_queried_service(incident, monkeypatch):
    monkeypatch.setattr(
        settings, "backend_logs_gcp_service_map", "test-service=run-svc"
    )
    captured = {}
    entries = [
        _entry(
            "from the mapped service",
            incident,
            resource={
                "type": "cloud_run_revision",
                "labels": {"service_name": "run-svc"},
            },
        )
    ]
    collector = _make_collector(_ok_handler(entries, captured))

    item = collector.collect(incident)[0]

    query_filter = captured["request"].url.params["filter"]
    assert 'resource.labels.service_name="run-svc"' in query_filter
    # The evidence still speaks the incident's service vocabulary, so
    # the shared service filter passes.
    assert "(test-service)" in item.content
    # The origin names the Cloud Run service that was actually queried.
    assert item.metadata["origin"].endswith("/services/run-svc")
    assert "run-svc" in str(item.metadata)


def test_alert_metadata_service_override_wins(incident, monkeypatch):
    monkeypatch.setattr(
        settings, "backend_logs_gcp_service_map", "test-service=mapped"
    )
    captured = {}
    overridden = incident.model_copy(
        update={"alert_metadata": {"cloud_run_service": "explicit-svc"}}
    )
    entries = [_entry("any line", overridden)]
    collector = _make_collector(_ok_handler(entries, captured))

    collector.collect(overridden)

    query_filter = captured["request"].url.params["filter"]
    assert 'resource.labels.service_name="explicit-svc"' in query_filter


# ---------------------------------------------------------------------------
# Explicit gap paths
# ---------------------------------------------------------------------------


def test_missing_adc_records_gap_without_any_request(incident):
    def provider():
        raise DefaultCredentialsError("no adc available")

    collector = GoogleCloudLoggingCollector(
        client=httpx.Client(transport=httpx.MockTransport(_no_request_handler)),
        credentials_provider=provider,
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert "Application Default Credentials" in evidence[0].description


def test_refresh_failure_records_gap_without_any_request(incident):
    collector = _make_collector(
        _no_request_handler,
        credentials=FakeCredentials(refresh_error=RefreshError("denied")),
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "RefreshError" in evidence[0].description


def test_missing_token_records_gap_without_any_request(incident):
    collector = _make_collector(
        _no_request_handler, credentials=FakeCredentials(token=None)
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "no access token" in evidence[0].description


def test_permission_denied_records_gap(incident):
    collector = _make_collector(
        lambda request: httpx.Response(403, json={"error": "forbidden"})
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "HTTP 403" in evidence[0].description
    assert "roles/logging.viewer" in evidence[0].description


def test_unauthorized_records_permission_gap(incident):
    collector = _make_collector(
        lambda request: httpx.Response(401, json={"error": "unauthorized"})
    )

    evidence = collector.collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "HTTP 401" in evidence[0].description


def test_timeout_records_gap(incident):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    evidence = _make_collector(handler).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "timeout" in evidence[0].description.lower()


def test_server_error_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(500, json={"error": "boom"})
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "HTTP 500" in evidence[0].description


def test_non_json_response_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(200, text="<html>oops</html>")
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "non-JSON" in evidence[0].description


def test_unexpected_shape_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(200, json={"unexpected": []})
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert "unexpected JSON shape" in evidence[0].description


def test_empty_entries_records_gap(incident):
    evidence = _make_collector(_ok_handler([])).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
    assert evidence[0].status == "missing"
    assert "no entries" in evidence[0].description.lower()


def test_entries_without_messages_are_dropped(incident):
    evidence = _make_collector(
        _ok_handler(
            [
                {
                    "logName": f"projects/{PROJECT}/logs/other",
                    "resource": {
                        "type": "cloud_run_revision",
                        "labels": {"service_name": "test-service"},
                    },
                }
            ]
        )
    ).collect(incident)

    assert evidence[0].id == "gap-backend-logs"
