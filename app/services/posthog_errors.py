"""PostHog frontend error evidence for investigations.

Collects frontend exception events (PostHog's ``$exception`` events from
exception autocapture, plus any event carrying an
``$exception_message`` property) for the incident's time window through
PostHog's Events API, and normalizes them into the shared
``BackendLogRecord`` pipeline from ``app.services.logs``: time-window,
severity, service and correlation filtering, redaction, the entry limit
and the byte limit are all reused, so frontend evidence obeys exactly
the same bounds as backend log evidence.

Correlation honesty: events are matched to the incident by time window,
service/environment properties, URLs, error messages, and any
correlation identifiers the frontend actually sent (``request_id``,
``trace_id``, ... including their ``$``-prefixed PostHog spellings,
applied by the shared correlation filter). Events that carry no
identifier cannot be disproven and are kept; nothing is *claimed* to be
caused by the incident - the model is told what was observed.

API access is isolated in this module: a single ``GET`` to
``{POSTHOG_HOST}/api/projects/{POSTHOG_PROJECT_ID}/events/`` with an
``Authorization: Bearer`` personal API token. The token is read only
from the environment, never logged and never stored in evidence.

Every failure mode - missing configuration, timeouts, permission
errors, malformed responses, and empty results - is reported through
the shared explicit ``missing`` gap mechanism, so the investigation
continues with its remaining evidence instead of failing or inventing
frontend errors.
"""

from datetime import datetime
from typing import Any

import httpx

from app.config import settings
from app.schemas.investigation import EvidenceItem, InvestigationInput
from app.services.logs import (
    BackendLogRecord,
    build_gap_evidence,
    build_log_evidence,
    incident_time_window,
    sanitize_url,
    select_records,
)

POSTHOG_ERRORS_EVIDENCE_ID = "posthog-errors"
POSTHOG_ERRORS_GAP_ID = "gap-posthog-errors"
POSTHOG_ERRORS_SOURCE = "frontend_errors"

# PostHog's event name for exception autocapture. Events under any other
# name are still treated as errors when they carry an
# ``$exception_message`` property, so a renamed or custom error event is
# not silently dropped.
ERROR_EVENT_NAME = "$exception"

# Candidate property spellings for the correlation identifiers the
# shared filter understands (mirroring CORRELATION_METADATA_KEYS from
# ``app.services.logs``), including PostHog's ``$``-prefixed convention.
_REQUEST_ID_KEYS = ("request_id", "requestId", "$request_id", "$requestId")
_TRACE_ID_KEYS = ("trace_id", "traceId", "$trace_id", "$traceId")


def _first_string(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _gap(description: str) -> list[EvidenceItem]:
    """The explicit PostHog gap, carrying this collector's stable IDs.

    Keyed on source ``frontend_errors`` so ``collect_evidence`` treats
    it as covering the frontend source (suppressing the static
    fallback) and citations stay unambiguous.
    """
    return build_gap_evidence(
        description,
        evidence_id=POSTHOG_ERRORS_GAP_ID,
        source=POSTHOG_ERRORS_SOURCE,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _compose_message(
    properties: dict[str, Any],
    event_name: str,
) -> str:
    """Build a factual one-line message from an error event."""
    exception_message = _first_string(properties, ("$exception_message",))
    exception_type = _first_string(properties, ("$exception_type",))

    if exception_message:
        if exception_type:
            return f"{exception_type}: {exception_message}"
        return exception_message

    # No exception message: fall back to the raw exception chain so the
    # evidence still shows what PostHog recorded, never a guess.
    exception_list = _first_string(properties, ("$exception_list",))
    if exception_list:
        return exception_list

    return f"PostHog event '{event_name}' flagged as an error event"


def _event_to_record(event: dict[str, Any]) -> BackendLogRecord | None:
    """Convert one PostHog event into a record; ``None`` if not usable.

    Non-error events are skipped, and an event without a parseable
    timestamp is dropped: it cannot be placed inside the incident
    window, so keeping it would claim a correlation the evidence does
    not support.
    """
    event_name = str(event.get("event") or "")
    properties = event.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    has_exception_message = bool(
        _first_string(properties, ("$exception_message",))
    )
    if event_name != ERROR_EVENT_NAME and not has_exception_message:
        return None

    timestamp = _parse_timestamp(event.get("timestamp"))
    if timestamp is None:
        return None

    metadata: dict[str, Any] = {"event": event_name}
    for key, prop_keys in (
        ("url", ("$current_url", "$page_location", "url")),
        ("environment", ("$environment", "environment")),
        ("hostname", ("$exception_hostname",)),
        ("fingerprint", ("$exception_fingerprint",)),
        ("session_id", ("$session_id",)),
    ):
        value = _first_string(properties, prop_keys)
        if value:
            metadata[key] = value

    distinct_id = event.get("distinct_id")
    if isinstance(distinct_id, str) and distinct_id.strip():
        # Pseudonymous client identifier (typically an anonymous UUID);
        # useful to group errors per client without exposing a user.
        metadata["distinct_id"] = distinct_id.strip()

    return BackendLogRecord(
        message=_compose_message(properties, event_name),
        level=_first_string(properties, ("$exception_level",)),
        timestamp=timestamp,
        # Only a service-named property counts as the service: an
        # environment like "production" is context, not a service match,
        # and is kept in metadata instead of being forced through the
        # service filter.
        service=_first_string(properties, ("service", "service_name")),
        request_id=_first_string(properties, _REQUEST_ID_KEYS),
        trace_id=_first_string(properties, _TRACE_ID_KEYS),
        span_id=_first_string(properties, ("$span_id", "span_id", "spanId")),
        logger_name=event_name or None,
        metadata=metadata,
    )


class PostHogErrorEvidenceCollector:
    """Frontend error evidence from PostHog's Events API.

    Configured with ``POSTHOG_HOST``, ``POSTHOG_PROJECT_ID`` and
    ``POSTHOG_API_KEY``; any missing value yields the explicit gap. One
    bounded page of at most ``BACKEND_LOGS_MAX_ENTRIES`` events is
    requested around the incident window, so one investigation cannot
    pull an unbounded amount of data.
    """

    name = "posthog_errors"

    def __init__(self, client: httpx.Client | None = None) -> None:
        # An injected client (e.g. with a mock transport) is not owned
        # or closed by the collector.
        self._client = client

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        host = settings.posthog_host.strip()
        project_id = settings.posthog_project_id.strip()
        api_key = settings.posthog_api_key.strip()

        if not (host and project_id and api_key):
            return _gap(
                "Frontend error collection from PostHog is not "
                "configured: POSTHOG_HOST, POSTHOG_PROJECT_ID and "
                "POSTHOG_API_KEY must all be set."
            )

        window_start, window_end = incident_time_window(incident)
        safe_host = sanitize_url(host)
        events_url = f"{safe_host}/api/projects/{project_id}/events/"
        params = {
            "after": window_start.isoformat(),
            "before": window_end.isoformat(),
            "event": ERROR_EVENT_NAME,
            "limit": str(settings.backend_logs_max_entries),
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=settings.backend_logs_timeout_seconds
        )

        try:
            response = client.get(events_url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            return _gap(
                f"The PostHog API at {safe_host} did not respond within "
                "the configured timeout; no frontend errors were "
                "collected."
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                return _gap(
                    f"The PostHog API at {safe_host} denied access "
                    f"(HTTP {status}); POSTHOG_API_KEY needs read access "
                    "to project events. No frontend errors were "
                    "collected."
                )
            if status == 404:
                return _gap(
                    f"The PostHog API at {safe_host} answered HTTP 404 "
                    f"for project '{project_id}'; check POSTHOG_HOST "
                    "(region/self-hosted URL) and POSTHOG_PROJECT_ID. "
                    "No frontend errors were collected."
                )
            return _gap(
                f"The PostHog API at {safe_host} answered with HTTP "
                f"{status}; no frontend errors were collected."
            )
        except httpx.HTTPError as exc:
            return _gap(
                f"The PostHog API at {safe_host} could not be reached "
                f"({type(exc).__name__}); no frontend errors were "
                "collected."
            )
        except ValueError:
            return _gap(
                f"The PostHog API at {safe_host} returned a non-JSON "
                "response; no frontend errors were collected."
            )
        finally:
            if owns_client:
                client.close()

        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"), list
        ):
            return _gap(
                f"The PostHog API at {safe_host} returned an unexpected "
                "JSON shape (expected an object with a 'results' "
                "array); no frontend errors were collected."
            )

        records: list[BackendLogRecord] = []
        for event in payload["results"]:
            if not isinstance(event, dict):
                continue
            record = _event_to_record(event)
            if record is not None:
                records.append(record)

        selected, window_start, window_end = select_records(
            records, incident
        )

        if not selected:
            return _gap(
                "PostHog was queried successfully but returned no "
                "frontend error events matching the incident time "
                "window and filters (severity/service/correlation) for "
                f"service '{incident.service_name}'."
            )

        return build_log_evidence(
            selected,
            window_start,
            window_end,
            origin=f"posthog:{safe_host}/projects/{project_id}",
            evidence_id=POSTHOG_ERRORS_EVIDENCE_ID,
            source=POSTHOG_ERRORS_SOURCE,
            label="frontend error",
        )
