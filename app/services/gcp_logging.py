"""Google Cloud Logging collector for backend log evidence.

Queries the Cloud Logging ``entries.list`` REST endpoint for the Cloud
Run service that backs the incident, then normalizes the returned
``LogEntry`` objects into the shared ``BackendLogRecord`` pipeline from
``app.services.logs``. Time-window selection, severity/service/
correlation filtering, redaction, the entry limit and the byte limit are
all reused from that module, so the GCP source behaves exactly like the
local and HTTP sources.

Authentication uses Application Default Credentials (ADC) with the
read-only scope ``https://www.googleapis.com/auth/logging.read``: on
Cloud Run that is the workload's own service account via the metadata
server; locally it is ``GOOGLE_APPLICATION_CREDENTIALS`` or ``gcloud
auth application-default login``. Nothing is hard-coded and the access
token is only ever placed in the request header - it is never logged and
never stored in evidence. The worker identity needs
``logging.logEntries.list`` on the configured project (for example
``roles/logging.viewer``).

Every failure mode - missing project configuration, missing ADC, a
failed credential refresh, permission errors, timeouts, malformed
responses, and empty results - is reported through the shared explicit
``missing`` gap mechanism instead of fabricated content.
"""

import json
from datetime import UTC, datetime
from typing import Any

import google.auth
import httpx
from google.auth.exceptions import (
    DefaultCredentialsError,
    RefreshError,
    TransportError,
)
from google.auth.transport.requests import Request as GoogleAuthRequest

from app.config import settings
from app.schemas.investigation import EvidenceItem, InvestigationInput
from app.services.logs import (
    BackendLogRecord,
    build_gap_evidence,
    build_log_evidence,
    incident_time_window,
    select_records,
)

LOGGING_READ_SCOPE = "https://www.googleapis.com/auth/logging.read"
ENTRIES_URL = "https://logging.googleapis.com/v1/entries"

# Cloud Logging severity names mapped to the lowercase levels used by
# the shared severity floor. ALERT and EMERGENCY are more severe than
# CRITICAL in Cloud Logging and are folded into it; DEFAULT means
# "unset" and stays unclassifiable (the shared floor keeps unknown
# levels on purpose).
_SEVERITY_TO_LEVEL = {
    "default": "default",
    "debug": "debug",
    "info": "info",
    "notice": "notice",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
    "alert": "critical",
    "emergency": "critical",
}

# The configured severity floor (``BACKEND_LOGS_MIN_LEVEL``) translated
# to the Cloud Logging vocabulary for the server-side filter. The floor
# is re-applied client-side by ``select_records`` on whatever comes back.
_SEVERITY_FLOOR = {
    "debug": "DEBUG",
    "info": "INFO",
    "notice": "NOTICE",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
    "fatal": "CRITICAL",
}

# Structured payloads without a message field are summarized as compact
# JSON; the shared per-line limit bounds this further when the record
# becomes evidence.
_MAX_PAYLOAD_MESSAGE_CHARS = 2_000


def _escape_filter_value(value: str) -> str:
    """Escape a value for interpolation into a Logging filter string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _rfc3339(value: datetime) -> str:
    """Render a timestamp the way the Logging filter grammar expects."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _first_string(
    mapping: dict[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _service_map() -> dict[str, str]:
    """Parse ``BACKEND_LOGS_GCP_SERVICE_MAP`` (``a=b,c=d``) into a dict.

    Malformed pairs (no ``=``) are ignored rather than failing an
    investigation over configuration bookkeeping.
    """
    mapping: dict[str, str] = {}
    raw = settings.backend_logs_gcp_service_map
    if not raw.strip():
        return mapping

    for pair in raw.split(","):
        key, separator, value = pair.partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key and value:
            mapping[key] = value

    return mapping


def resolve_cloud_run_service(incident: InvestigationInput) -> str:
    """Cloud Run service name whose logs should be collected.

    Precedence: an explicit ``cloud_run_service`` key in the alert
    metadata (reusing existing incident metadata), then the optional
    ``BACKEND_LOGS_GCP_SERVICE_MAP`` mapping, then the incident's own
    service name, which doubles as the Cloud Run service name by
    default.
    """
    explicit = incident.alert_metadata.get("cloud_run_service", "").strip()
    if explicit:
        return explicit

    mapped = _service_map().get(incident.service_name.strip())
    if mapped:
        return mapped

    return incident.service_name.strip()


def build_filter_query(
    service: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Server-side Cloud Logging filter for one investigation.

    The filter pushes down everything Cloud Logging expresses well -
    the Cloud Run resource, the service name, the incident time window
    and the configured severity floor - so the API returns a bounded,
    relevant set. Correlation identifiers are deliberately *not* part of
    the server-side filter: a ``trace:`` restriction would also drop the
    identifier-less lines the shared client-side filter keeps on
    purpose; the shared filters re-apply window/severity/service/
    correlation selection on whatever comes back.
    """
    lines = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{_escape_filter_value(service)}"',
        f'timestamp>="{_rfc3339(window_start)}"',
        f'timestamp<="{_rfc3339(window_end)}"',
    ]

    severity_floor = _SEVERITY_FLOOR.get(
        settings.backend_logs_min_level.strip().lower()
    )
    if severity_floor:
        lines.append(f"severity>={severity_floor}")

    return "\n".join(lines)


def _entry_message(entry: dict[str, Any]) -> str:
    """Extract the human-readable message from a ``LogEntry``."""
    text = entry.get("textPayload")
    if isinstance(text, str) and text.strip():
        return text

    for key in ("jsonPayload", "protoPayload"):
        payload = entry.get(key)
        if not isinstance(payload, dict):
            continue
        message = payload.get("message") or payload.get("msg")
        if isinstance(message, str) and message.strip():
            return message
        # Structured payloads without a message field (including audit
        # proto payloads) are summarized as compact JSON.
        rendered = json.dumps(payload, default=str)
        if len(rendered) > _MAX_PAYLOAD_MESSAGE_CHARS:
            return rendered[:_MAX_PAYLOAD_MESSAGE_CHARS] + "... [truncated]"
        return rendered

    return ""


def _entry_timestamp(entry: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "receiveTimestamp"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return None


def _bare_trace_id(value: Any) -> str | None:
    """Reduce a full trace resource name to its bare trace ID.

    Cloud Logging stores ``trace`` as ``projects/P/traces/TRACE_ID``
    while alerts carry the bare trace ID, so the prefix is stripped for
    correlation to work.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    value = value.strip()
    marker = "/traces/"
    if marker in value:
        return value.rsplit(marker, 1)[1]
    return value


def _entry_to_record(
    entry: dict[str, Any],
    incident_service: str,
) -> BackendLogRecord | None:
    """Convert one Cloud Logging ``LogEntry`` (REST JSON) into a record.

    Returns ``None`` when the entry carries no renderable message (for
    example an entry with only resource bookkeeping fields).
    """
    message = _entry_message(entry)
    if not message:
        return None

    json_payload = entry.get("jsonPayload")
    if not isinstance(json_payload, dict):
        json_payload = {}

    resource = entry.get("resource")
    resource_labels = (
        resource.get("labels") if isinstance(resource, dict) else None
    )
    run_service = (
        resource_labels.get("service_name")
        if isinstance(resource_labels, dict)
        else None
    )

    metadata: dict[str, Any] = {}
    log_name = entry.get("logName")
    if isinstance(log_name, str) and log_name:
        metadata["log_name"] = log_name
    if isinstance(run_service, str) and run_service:
        metadata["cloud_run_service"] = run_service

    severity = str(entry.get("severity") or "").strip().lower()
    level = _SEVERITY_TO_LEVEL.get(severity, severity or None)

    request_id = _first_string(json_payload, ("request_id", "requestId"))
    if request_id is None:
        labels = entry.get("labels")
        if isinstance(labels, dict):
            request_id = _first_string(labels, ("request_id", "requestId"))

    return BackendLogRecord(
        message=message,
        level=level,
        timestamp=_entry_timestamp(entry),
        # The API filter already restricted the query to the incident's
        # Cloud Run service, so the record keeps the incident's own
        # service name: the shared service filter (and the report) then
        # speak the incident's vocabulary even when a mapping renamed
        # the Cloud Run service.
        service=incident_service or run_service,
        request_id=request_id,
        trace_id=_bare_trace_id(entry.get("trace"))
        or _first_string(json_payload, ("trace_id", "traceId")),
        span_id=_first_string(entry, ("spanId",))
        or _first_string(json_payload, ("span_id", "spanId")),
        metadata=metadata or None,
    )


def _adc_credentials() -> Any:
    """Return Application Default Credentials scoped to log reads."""
    credentials, _project = google.auth.default(scopes=(LOGGING_READ_SCOPE,))
    return credentials


class GoogleCloudLoggingCollector:
    """Backend log evidence from Google Cloud Logging (Cloud Run logs).

    Selected with ``BACKEND_LOGS_SOURCE=gcp``. The project comes from
    the existing ``GCP_PROJECT_ID`` setting; the Cloud Run service is
    resolved with ``resolve_cloud_run_service``. Credentials come from
    ADC (see the module docstring) and may be injected for tests
    together with an ``httpx`` client. A single page of at most
    ``BACKEND_LOGS_MAX_ENTRIES`` entries is requested, so one
    investigation stays bounded.
    """

    name = "backend_logs_gcp"

    def __init__(
        self,
        client: httpx.Client | None = None,
        credentials_provider: Any = None,
    ) -> None:
        # An injected client (e.g. with a mock transport) is not owned
        # or closed by the collector, and an injected credentials
        # provider replaces Application Default Credentials (tests).
        self._client = client
        self._credentials_provider = credentials_provider or _adc_credentials

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        project = settings.gcp_project_id.strip()
        if not project:
            return build_gap_evidence(
                "Google Cloud Logging collection is not configured: "
                "GCP_PROJECT_ID is empty, so no project could be "
                "queried."
            )

        service = resolve_cloud_run_service(incident)
        if not service:
            return build_gap_evidence(
                "The incident provides no service name (and no "
                "cloud_run_service alert metadata), so the Cloud Run "
                "service to query in Google Cloud Logging could not be "
                "determined."
            )

        try:
            credentials = self._credentials_provider()
            credentials.refresh(GoogleAuthRequest())
        except DefaultCredentialsError:
            return build_gap_evidence(
                "Application Default Credentials are not available, so "
                "Google Cloud Logging could not be queried (no logs "
                "were collected)."
            )
        except (RefreshError, TransportError) as exc:
            return build_gap_evidence(
                "Refreshing Application Default Credentials failed "
                f"({type(exc).__name__}), so Google Cloud Logging could "
                "not be queried (no logs were collected)."
            )

        token = getattr(credentials, "token", None)
        if not token:
            return build_gap_evidence(
                "Application Default Credentials produced no access "
                "token, so Google Cloud Logging could not be queried "
                "(no logs were collected)."
            )

        window_start, window_end = incident_time_window(incident)
        params = [
            ("resourceNames", f"projects/{project}"),
            (
                "filter",
                build_filter_query(service, window_start, window_end),
            ),
            ("orderBy", "timestamp asc"),
            ("pageSize", str(settings.backend_logs_max_entries)),
        ]
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=settings.backend_logs_timeout_seconds
        )

        try:
            response = client.get(
                ENTRIES_URL, params=params, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            return build_gap_evidence(
                "The Google Cloud Logging API did not respond within "
                "the configured timeout; no logs were collected."
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                return build_gap_evidence(
                    f"The Google Cloud Logging API denied access "
                    f"(HTTP {status}) for project '{project}'; the "
                    "worker identity needs logging.logEntries.list "
                    "(for example roles/logging.viewer). No logs were "
                    "collected."
                )
            return build_gap_evidence(
                f"The Google Cloud Logging API answered with HTTP "
                f"{status} for project '{project}'; no logs were "
                "collected."
            )
        except httpx.HTTPError as exc:
            return build_gap_evidence(
                "The Google Cloud Logging API could not be reached "
                f"({type(exc).__name__}); no logs were collected."
            )
        except ValueError:
            return build_gap_evidence(
                "The Google Cloud Logging API returned a non-JSON "
                "response; no logs were collected."
            )
        finally:
            if owns_client:
                client.close()

        if not isinstance(payload, dict) or not isinstance(
            payload.get("entries"), list
        ):
            return build_gap_evidence(
                "The Google Cloud Logging API returned an unexpected "
                "JSON shape (expected an object with an 'entries' "
                "array); no logs were collected."
            )

        records: list[BackendLogRecord] = []
        for entry in payload["entries"]:
            if not isinstance(entry, dict):
                continue
            record = _entry_to_record(entry, incident.service_name)
            if record is not None:
                records.append(record)

        selected, window_start, window_end = select_records(
            records, incident
        )

        if not selected:
            return build_gap_evidence(
                "Google Cloud Logging was queried successfully but "
                "returned no entries matching the incident time window "
                "and filters (severity/service/correlation) for "
                f"service '{service}'."
            )

        return build_log_evidence(
            selected,
            window_start,
            window_end,
            origin=f"gcp:projects/{project}/services/{service}",
        )
