"""Tests for the PostHog frontend error evidence collector.

Everything runs offline: the HTTP call goes through an ``httpx.MockTransport``
and no request ever leaves the process, so the real PostHog API is never
contacted. Covered: unconfigured/partial configuration gaps, request and
filter construction (window, event name, limit, bearer token), event
normalization (exception message/type/level, service, correlation
identifiers, URLs), client-side re-filtering, redaction, entry limits,
the explicit gap paths (timeout, 401/403, 404, other HTTP errors,
non-JSON, unexpected shape, empty results), replacement of the static
frontend-gap fallback, and the engine's default collector wiring.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import settings
from app.schemas.investigation import InvestigationInput
from app.services.evidence import (
    AlertPayloadEvidenceCollector,
    collect_evidence,
)
from app.services.posthog_errors import (
    POSTHOG_ERRORS_EVIDENCE_ID,
    POSTHOG_ERRORS_GAP_ID,
    POSTHOG_ERRORS_SOURCE,
    PostHogErrorEvidenceCollector,
)

HOST = "https://us.i.posthog.com"
PROJECT = "7"
TOKEN = "phx_test_token_not_real"


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
def posthog_config(monkeypatch):
    monkeypatch.setattr(settings, "posthog_host", HOST)
    monkeypatch.setattr(settings, "posthog_project_id", PROJECT)
    monkeypatch.setattr(settings, "posthog_api_key", TOKEN)


def _make_collector(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PostHogErrorEvidenceCollector(client=client)


def _timestamp(incident, minutes=0):
    moment = incident.last_seen_at.replace(tzinfo=UTC) + timedelta(
        minutes=minutes
    )
    return moment.isoformat()


def _event(
    incident,
    *,
    minutes=0,
    name="$exception",
    message="TypeError: x is undefined",
    exception_type="TypeError",
    level="error",
    timestamp=None,
    **extra_properties,
):
    properties = {
        "$exception_message": message,
        "$exception_type": exception_type,
        "$exception_level": level,
        "$current_url": "https://app.example.com/checkout",
        "$environment": "production",
        "service": "test-service",
    }
    properties.update(extra_properties)
    return {
        "uuid": str(uuid.uuid4()),
        "event": name,
        "timestamp": timestamp or _timestamp(incident, minutes),
        "properties": properties,
        "distinct_id": "anon-uuid-1",
    }


def _pageview(incident, *, minutes=0):
    return {
        "uuid": str(uuid.uuid4()),
        "event": "$pageview",
        "timestamp": _timestamp(incident, minutes),
        "properties": {"$current_url": "https://app.example.com/"},
        "distinct_id": "anon-uuid-1",
    }


def _ok_handler(results, captured=None):
    def handler(request):
        if captured is not None:
            captured["request"] = request
        return httpx.Response(200, json={"results": results})

    return handler


def _no_request_handler(request):
    pytest.fail("no HTTP request should be made in this test")


# ---------------------------------------------------------------------------
# Configuration and gap replacement
# ---------------------------------------------------------------------------


def test_unconfigured_records_gap_without_any_request(incident, monkeypatch):
    monkeypatch.setattr(settings, "posthog_host", "")
    collector = _make_collector(_no_request_handler)

    evidence = collector.collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert evidence[0].source == POSTHOG_ERRORS_SOURCE
    assert evidence[0].status == "missing"
    assert "POSTHOG_HOST" in evidence[0].description


def test_partially_configured_records_gap_without_any_request(
    incident, monkeypatch
):
    monkeypatch.setattr(settings, "posthog_api_key", "")
    collector = _make_collector(_no_request_handler)

    evidence = collector.collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert evidence[0].status == "missing"


def test_collector_gap_replaces_the_static_frontend_gap(incident):
    items = collect_evidence(
        incident,
        collectors=(AlertPayloadEvidenceCollector(),
                    PostHogErrorEvidenceCollector()),
    )
    ids = [item.id for item in items]

    # The collector's own explicit gap is present exactly once and the
    # static fallback (same source) is suppressed.
    assert ids.count(POSTHOG_ERRORS_GAP_ID) == 1
    assert "gap-frontend-errors" not in ids


# ---------------------------------------------------------------------------
# Successful collection
# ---------------------------------------------------------------------------


def test_collects_and_normalizes_error_events(incident):
    captured = {}
    results = [
        _event(incident, minutes=-5),
        _event(
            incident,
            minutes=-3,
            message="payment gateway timed out",
            exception_type="PaymentError",
            **{"$trace_id": "trace-abc", "$span_id": "span-1"},
        ),
    ]
    collector = _make_collector(_ok_handler(results, captured))

    evidence = collector.collect(incident)

    request = captured["request"]
    assert request.method == "GET"
    assert request.url.host == "us.i.posthog.com"
    assert request.url.path == f"/api/projects/{PROJECT}/events/"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.url.params["event"] == "$exception"
    assert request.url.params["limit"] == str(
        settings.backend_logs_max_entries
    )
    window_start = incident.last_seen_at.replace(tzinfo=UTC) - timedelta(
        minutes=settings.backend_logs_window_minutes_before
    )
    window_end = incident.last_seen_at.replace(tzinfo=UTC) + timedelta(
        minutes=settings.backend_logs_window_minutes_after
    )
    assert request.url.params["after"] == window_start.isoformat()
    assert request.url.params["before"] == window_end.isoformat()

    assert len(evidence) == 1
    item = evidence[0]
    assert item.id == POSTHOG_ERRORS_EVIDENCE_ID
    assert item.source == POSTHOG_ERRORS_SOURCE
    assert item.status == "available"
    assert "TypeError: x is undefined" in item.content
    assert "PaymentError: payment gateway timed out" in item.content
    assert "[ERROR]" in item.content
    # Service comes from the event's own service property.
    assert "(test-service)" in item.content
    assert "trace=trace-abc" in item.content
    assert "span-1" in str(item.metadata)
    assert item.metadata["entry_count"] == "2"
    assert item.metadata["origin"] == (
        f"posthog:{HOST}/projects/{PROJECT}"
    )
    assert "app.example.com/checkout" in str(item.metadata)
    # Chronological order.
    assert (
        item.content.index("TypeError")
        < item.content.index("PaymentError")
    )


def test_request_id_property_is_surfaced(incident):
    results = [_event(incident, request_id="req-77")]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(incident)[0]

    assert "req=req-77" in item.content


def test_custom_event_with_exception_message_is_kept(incident):
    results = [
        _pageview(incident),
        _event(incident, name="checkout_failed", message="cart expired"),
    ]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(incident)[0]

    assert "cart expired" in item.content
    assert "$pageview" not in item.content


def test_event_without_parseable_timestamp_is_dropped(incident):
    results = [_event(incident, timestamp="not-a-date")]
    collector = _make_collector(_ok_handler(results))

    evidence = collector.collect(incident)

    # Nothing could be placed inside the incident window, honestly.
    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert evidence[0].status == "missing"


def test_service_mismatch_is_filtered_and_untagged_is_kept(incident):
    results = [
        _event(incident, message="ours failed"),
        _event(incident, message="other service failed", service="other"),
        _event(incident, message="untagged failure", service=""),
    ]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(incident)[0]

    assert "ours failed" in item.content
    # Events without a service tag cannot be disproven; they stay.
    assert "untagged failure" in item.content
    assert "other service failed" not in item.content


def test_correlation_identifiers_filter_events(incident):
    correlated = incident.model_copy(
        update={"alert_metadata": {"trace_id": "trace-abc"}}
    )
    results = [
        _event(correlated, message="matches", **{"$trace_id": "trace-abc"}),
        _event(correlated, message="other trace", **{"$trace_id": "trace-zzz"}),
        _event(correlated, message="no identifiers"),
    ]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(correlated)[0]

    assert "matches" in item.content
    # Identifier-less events cannot be disproven; they stay.
    assert "no identifiers" in item.content
    assert "other trace" not in item.content


def test_severity_floor_drops_lower_exceptions(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_min_level", "error")
    results = [
        _event(incident, message="a hard failure", level="error"),
        _event(incident, message="merely a warning", level="warning"),
    ]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(incident)[0]

    assert "a hard failure" in item.content
    assert "merely a warning" not in item.content


def test_entry_limit_bounds_results(incident, monkeypatch):
    monkeypatch.setattr(settings, "backend_logs_max_entries", 2)
    captured = {}
    results = [
        _event(incident, minutes=-index, message=f"failure {index}")
        for index in range(4)
    ]
    collector = _make_collector(_ok_handler(results, captured))

    item = collector.collect(incident)[0]

    assert captured["request"].url.params["limit"] == "2"
    assert item.metadata["entry_count"] == "2"


def test_redaction_applies_to_frontend_errors(incident):
    results = [
        _event(
            incident,
            message="request failed with api_key=super-secret-value",
        )
    ]
    collector = _make_collector(_ok_handler(results))

    item = collector.collect(incident)[0]

    assert "[REDACTED]" in item.content
    assert "super-secret-value" not in item.content


def test_api_token_is_never_stored_in_evidence(incident):
    collector = _make_collector(_ok_handler([_event(incident)]))

    item = collector.collect(incident)[0]

    blob = (
        item.description
        + (item.content or "")
        + " ".join(item.metadata.values())
    )
    assert TOKEN not in blob


# ---------------------------------------------------------------------------
# Explicit gap paths
# ---------------------------------------------------------------------------


def test_timeout_records_gap(incident):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    evidence = _make_collector(handler).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "timeout" in evidence[0].description.lower()


def test_unauthorized_records_credential_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(401, json={"detail": "invalid"})
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "HTTP 401" in evidence[0].description
    assert "POSTHOG_API_KEY" in evidence[0].description


def test_forbidden_records_credential_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(403, json={"detail": "forbidden"})
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "HTTP 403" in evidence[0].description


def test_not_found_records_gap_naming_project_and_host(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(404, json={"detail": "not found"})
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "HTTP 404" in evidence[0].description
    assert "POSTHOG_PROJECT_ID" in evidence[0].description


def test_server_error_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(500, json={"detail": "boom"})
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "HTTP 500" in evidence[0].description


def test_non_json_response_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(200, text="<html>oops</html>")
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "non-JSON" in evidence[0].description


def test_unexpected_shape_records_gap(incident):
    evidence = _make_collector(
        lambda request: httpx.Response(200, json={"count": 0})
    ).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert "unexpected JSON shape" in evidence[0].description


def test_empty_results_records_gap(incident):
    evidence = _make_collector(_ok_handler([])).collect(incident)

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert evidence[0].status == "missing"
    assert "no frontend error events" in evidence[0].description.lower()


def test_only_non_error_events_records_gap(incident):
    evidence = _make_collector(_ok_handler([_pageview(incident)])).collect(
        incident
    )

    assert evidence[0].id == POSTHOG_ERRORS_GAP_ID
    assert evidence[0].status == "missing"


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def test_engine_default_collectors_include_posthog_gap():
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

        # Default collector set: PostHog is unconfigured in the test
        # environment, so the run records its explicit gap alongside
        # the backend-log gap and the alert evidence.
        row = run_investigation(
            db,
            job=job,
            incident=incident,
            provider=FakeProvider(valid_findings()),
        )

        evidence_ids = {item["id"] for item in row.evidence}
        assert POSTHOG_ERRORS_GAP_ID in evidence_ids
        assert "gap-backend-logs" in evidence_ids
        assert {"alert-summary", "alert-metadata"} <= evidence_ids
        assert POSTHOG_ERRORS_EVIDENCE_ID not in evidence_ids
    finally:
        db.close()
