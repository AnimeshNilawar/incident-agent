"""Backend log collection for investigations.

Evidence sources implement the small ``EvidenceCollector`` protocol from
``app.services.evidence``. This module provides:

* ``BackendLogRecord``      - a normalized, timezone-aware log line.
* ``LocalInMemoryBackendLogCollector`` - a test/local collector that
  applies the exact same filtering and formatting a real source would.
* ``HTTPBackendLogCollector`` - a generic HTTP log source (any endpoint
  that answers with JSON entries). It is source-agnostic on purpose: the
  actual backend log system (Cloud Logging, Loki, an in-house API, ...)
  is connected later by pointing ``BACKEND_LOGS_URL`` at it, or by
  adding a dedicated collector class next to this one.
* ``NoneBackendLogCollector`` - explicitly records the source as missing.
* ``app.services.gcp_logging.GoogleCloudLoggingCollector`` - Google
  Cloud Logging (Cloud Run service logs) via Application Default
  Credentials, selected with ``BACKEND_LOGS_SOURCE=gcp``. The module
  lives next to this one so the source-specific code stays separate.

Two rules keep reports honest and bounded:

* Every collected item has a stable ID (``backend-logs`` /
  ``gap-backend-logs``) so citations and the dashboard can reference it.
* Selection is always bounded: incident time window, optional severity
  floor, service and correlation-identifier filters, an entry limit and
  a total payload byte limit. Anything that cannot be fetched becomes an
  explicit ``missing`` gap - never fabricated content.

Secrets: log messages and metadata are redacted with configurable
patterns before they become evidence, and the HTTP collector never logs
or stores its bearer token (only sanitized URLs and error class names
are reported).
"""

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import settings
from app.schemas.investigation import EvidenceItem, InvestigationInput

logger = logging.getLogger(__name__)

# Stable evidence IDs. They are load-bearing: the engine validates that
# every LLM citation refers to an item that was actually collected.
BACKEND_LOGS_EVIDENCE_ID = "backend-logs"
BACKEND_LOGS_GAP_ID = "gap-backend-logs"

MAX_LINE_CHARS = 4_000

# Simple redaction patterns for common sensitive tokens. Additional
# patterns can be configured via ``BACKEND_LOGS_REDACT_PATTERNS``
# (comma-separated regex strings).
DEFAULT_REDACT_PATTERNS = [
    r"(?i)password\s*[=:]\s*\S+",
    r"(?i)passphrase\s*[=:]\s*\S+",
    r"(?i)secret\s*[=:]\s*\S+",
    r"(?i)api[_-]?key\s*[=:]\s*\S+",
    r"(?i)token\s*[=:]\s*\S+",
    r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?\S+",
    r"(?i)cookie\s*[:=]\s*\S+",
    r"(?i)set-cookie\s*[:=]\s*\S+",
    r"\b(?:sk|rk|pk)[_-][A-Za-z0-9_-]{20,}\b",
]

# Ordering used for the optional severity floor. Levels outside this
# map are treated as unclassifiable and are always kept (a log line of
# unknown severity may well be the most relevant one).
LEVEL_RANKS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "notice": 25,
    "warn": 30,
    "warning": 30,
    "error": 40,
    "critical": 50,
    "fatal": 50,
}

# Alert metadata keys that may carry correlation identifiers. When any
# of them is present on the incident, log records that carry their own
# identifiers are only kept when they match.
CORRELATION_METADATA_KEYS = (
    "request_id",
    "requestId",
    "trace_id",
    "traceId",
    "correlation_id",
    "correlationId",
)


class BackendLogRecord:
    """Normalized representation of a single backend log line.

    Timestamps are stored as timezone-aware UTC; a naive timestamp is
    interpreted as UTC.
    """

    def __init__(
        self,
        message: str,
        level: str | None = None,
        timestamp: datetime | None = None,
        service: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        logger_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.level = (level or "unknown").strip().lower() or "unknown"
        self.timestamp = self._normalize_timestamp(timestamp)
        self.service = service
        self.request_id = request_id
        self.trace_id = trace_id
        self.span_id = span_id
        self.logger_name = logger_name
        self.metadata = dict(metadata or {})

    @staticmethod
    def _normalize_timestamp(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)

        return value.astimezone(UTC)


def _compile_redaction_patterns() -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for pattern in DEFAULT_REDACT_PATTERNS:
        try:
            patterns.append(re.compile(pattern))
        except re.error:  # pragma: no cover - defaults are valid
            continue

    extra = settings.backend_logs_redact_patterns
    if extra:
        for part in extra.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                patterns.append(re.compile(part))
            except re.error:
                # A malformed user pattern must not crash the collector;
                # it is dropped (and the omission only ever *reduces*
                # redaction of already-redacted content, never exposes
                # something the defaults would have caught).
                logger.warning(
                    "ignoring invalid BACKEND_LOGS_REDACT_PATTERNS entry"
                )
                continue

    return patterns


_REDACTION_PATTERNS = _compile_redaction_patterns()


def redact_text(text: str) -> str:
    """Replace sensitive-looking tokens with ``[REDACTED]``.

    Used wherever free text becomes visible outside the originating
    system: log/evidence content at collection time and job error
    messages read back through the dashboard API.
    """
    result = text
    for pattern in _REDACTION_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _truncate_to_line_limit(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + "... [truncated line]"


def _format_log_entry(record: BackendLogRecord) -> str:
    parts: list[str] = [record.timestamp.isoformat(), f"[{record.level.upper()}]"]
    if record.service:
        parts.append(f"({record.service})")
    if record.request_id:
        parts.append(f"req={record.request_id}")
    if record.trace_id:
        parts.append(f"trace={record.trace_id}")

    message = _truncate_to_line_limit(redact_text(record.message))
    return f"{' '.join(parts)} {message}"


def _log_entry_to_dict(record: BackendLogRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp.isoformat(),
        "level": record.level,
        "service": record.service,
        "request_id": record.request_id,
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "logger": record.logger_name,
        "message": _truncate_to_line_limit(redact_text(record.message)),
        "metadata": {
            key: _truncate_to_line_limit(redact_text(str(value)))
            for key, value in record.metadata.items()
        },
    }


class BackendLogCollector(Protocol):
    """A source of backend log evidence for one investigation."""

    name: str

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        """Return evidence items containing backend logs (or a gap)."""
        ...


# ---------------------------------------------------------------------------
# Shared selection / formatting logic
# ---------------------------------------------------------------------------


def incident_time_window(
    incident: InvestigationInput,
) -> tuple[datetime, datetime]:
    """Bounded window around the incident's last_seen_at."""
    anchor = incident.last_seen_at.replace(tzinfo=UTC)
    window_start = anchor - timedelta(
        minutes=settings.backend_logs_window_minutes_before
    )
    window_end = anchor + timedelta(
        minutes=settings.backend_logs_window_minutes_after
    )
    return window_start, window_end


def incident_correlation_ids(incident: InvestigationInput) -> set[str]:
    """Correlation identifiers that arrived with the triggering alert."""
    ids: set[str] = set()
    for key in CORRELATION_METADATA_KEYS:
        value = incident.alert_metadata.get(key, "").strip()
        if value:
            ids.add(value)
    return ids


def _passes_severity_floor(record: BackendLogRecord) -> bool:
    min_level = settings.backend_logs_min_level.strip().lower()
    if not min_level:
        return True

    record_rank = LEVEL_RANKS.get(record.level)
    if record_rank is None:
        # Unclassifiable level: keep it, dropping it could hide the
        # most relevant line.
        return True

    return record_rank >= LEVEL_RANKS[min_level]


def _passes_service_filter(record: BackendLogRecord, incident: InvestigationInput) -> bool:
    if not record.service or not incident.service_name:
        return True
    return (
        record.service.strip().lower()
        == incident.service_name.strip().lower()
    )


def _passes_correlation_filter(
    record: BackendLogRecord,
    correlation_ids: set[str],
) -> bool:
    if not correlation_ids:
        return True

    record_ids = {
        value.strip()
        for value in (record.request_id, record.trace_id)
        if value and value.strip()
    }
    # Records without any identifier cannot be disproven; keep them.
    if not record_ids:
        return True

    return bool(record_ids & correlation_ids)


def select_records(
    records: Sequence[BackendLogRecord],
    incident: InvestigationInput,
) -> tuple[list[BackendLogRecord], datetime, datetime]:
    """Apply window, severity, service and correlation filters.

    Returns the selected records (chronologically sorted, entry-limited)
    together with the effective time window for provenance.
    """
    window_start, window_end = incident_time_window(incident)
    correlation_ids = incident_correlation_ids(incident)

    candidates = [
        record
        for record in records
        if window_start <= record.timestamp <= window_end
        and _passes_severity_floor(record)
        and _passes_service_filter(record, incident)
        and _passes_correlation_filter(record, correlation_ids)
    ]

    candidates.sort(key=lambda record: record.timestamp)
    return (
        candidates[: settings.backend_logs_max_entries],
        window_start,
        window_end,
    )


def build_gap_evidence(
    description: str,
    *,
    evidence_id: str = BACKEND_LOGS_GAP_ID,
    source: str = "backend_logs",
) -> list[EvidenceItem]:
    """The standard explicit gap item for an evidence source.

    The ID and source are parameters so other collectors (for example
    the PostHog frontend error collector) can record their own
    source-keyed gap without colliding with backend-log citations.
    """
    return [
        EvidenceItem(
            id=evidence_id,
            source=source,
            description=description[:500],
            status="missing",
        )
    ]


def build_log_evidence(
    selected: Sequence[BackendLogRecord],
    window_start: datetime,
    window_end: datetime,
    *,
    origin: str,
    evidence_id: str = BACKEND_LOGS_EVIDENCE_ID,
    source: str = "backend_logs",
    label: str = "backend log",
) -> list[EvidenceItem]:
    """Render selected records as one bounded, redacted evidence item.

    The stable ID, source and label are parameters so other collectors
    (for example the PostHog frontend error collector) can reuse this
    exact formatting, redaction and limit pipeline while keeping their
    own citation IDs.
    """
    content = "\n".join(_format_log_entry(record) for record in selected)

    # Enforce the total payload limit in bytes (UTF-8), truncating on a
    # character boundary so the evidence stays valid text.
    encoded = content.encode("utf-8")
    if len(encoded) > settings.backend_logs_max_bytes:
        content = (
            encoded[: settings.backend_logs_max_bytes].decode(
                "utf-8", errors="ignore"
            )
            + "\n...[truncated by byte limit]"
        )

    entries = [_log_entry_to_dict(record) for record in selected]
    count = len(selected)

    return [
        EvidenceItem(
            id=evidence_id,
            source=source,
            description=(
                f"{count} {label} entr{'y' if count == 1 else 'ies'} "
                f"collected between {window_start.isoformat()} and "
                f"{window_end.isoformat()} (max "
                f"{settings.backend_logs_max_entries} entries, source: "
                f"{origin})."
            ),
            status="available",
            content=content,
            observed_at=selected[-1].timestamp,
            metadata={
                "origin": origin,
                "entry_count": str(count),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "entries": str(entries),
            },
        )
    ]


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


class LocalInMemoryBackendLogCollector:
    """In-memory collector for tests and local development.

    Applies the same window/severity/service/correlation selection and
    the same redaction and limits as a real source, so tests exercise
    the production behaviour. Populate it via ``add_log`` /
    ``set_logs``; an empty or non-matching set yields the explicit gap.
    """

    name = "backend_logs_local"

    def __init__(self, logs: Sequence[BackendLogRecord] | None = None) -> None:
        self._logs: list[BackendLogRecord] = list(logs or [])

    def add_log(self, record: BackendLogRecord) -> None:
        self._logs.append(record)

    def set_logs(self, logs: Sequence[BackendLogRecord]) -> None:
        self._logs = list(logs)

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        selected, window_start, window_end = select_records(
            self._logs, incident
        )

        if not selected:
            return build_gap_evidence(
                "No backend logs matched the incident time window and "
                "filters (severity/service/correlation) for the affected "
                "service; the log source was queried but returned no "
                "relevant entries."
            )

        return build_log_evidence(
            selected,
            window_start,
            window_end,
            origin=self.name,
        )


class HTTPBackendLogCollector:
    """Generic HTTP log source.

    Expected contract (deliberately minimal and source-agnostic):

    ``GET BACKEND_LOGS_URL?service=...&start=...&end=...&levels=...&limit=N``

    returning either a JSON array of entry objects or an object with an
    ``entries`` array. Entry objects are normalized with
    ``normalize_logs_to_records``. An optional bearer token
    (``BACKEND_LOGS_AUTH_TOKEN``) is sent as ``Authorization: Bearer
    ...``; it is never logged or stored in evidence.

    When the URL is not configured, or the request fails, an explicit
    ``missing`` gap is returned so the investigation stays honest. To
    connect a real backend log system later, either point the URL at an
    adapter for that system or add a dedicated collector class here and
    register it in ``build_backend_log_collector``.
    """

    name = "backend_logs_http"

    def __init__(self, client: httpx.Client | None = None) -> None:
        # An injected client (e.g. with a mock transport) is not owned
        # or closed by the collector.
        self._client = client

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        url = settings.backend_logs_url.strip()
        token = settings.backend_logs_auth_token.strip()

        if not url:
            return build_gap_evidence(
                "Backend log collection via HTTP is not configured: "
                "BACKEND_LOGS_URL is empty, so no log source could be "
                "queried."
            )

        window_start, window_end = incident_time_window(incident)
        params = {
            "service": incident.service_name,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "limit": str(settings.backend_logs_max_entries),
        }
        if settings.backend_logs_min_level.strip():
            params["levels"] = settings.backend_logs_min_level.strip().lower()

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        safe_url = sanitize_url(url)
        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=settings.backend_logs_timeout_seconds
        )

        try:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            return build_gap_evidence(
                f"The backend log source at {safe_url} did not respond "
                "within the configured timeout; no logs were collected."
            )
        except httpx.HTTPStatusError as exc:
            return build_gap_evidence(
                f"The backend log source at {safe_url} answered with "
                f"HTTP {exc.response.status_code}; no logs were collected."
            )
        except httpx.HTTPError as exc:
            return build_gap_evidence(
                f"The backend log source at {safe_url} could not be "
                f"reached ({type(exc).__name__}); no logs were collected."
            )
        except ValueError:
            return build_gap_evidence(
                f"The backend log source at {safe_url} returned a "
                "non-JSON response; no logs were collected."
            )
        finally:
            if owns_client:
                client.close()

        raw_entries = _extract_entries(payload)
        if raw_entries is None:
            return build_gap_evidence(
                f"The backend log source at {safe_url} returned an "
                "unexpected JSON shape (expected an array of entries or "
                "an object with an 'entries' array); no logs were "
                "collected."
            )

        records = normalize_logs_to_records(raw_entries)
        selected, window_start, window_end = select_records(
            records, incident
        )

        if not selected:
            return build_gap_evidence(
                "The backend log source was queried successfully but "
                "returned no entries matching the incident time window "
                "and filters (severity/service/correlation)."
            )

        return build_log_evidence(
            selected,
            window_start,
            window_end,
            origin=safe_url,
        )


def sanitize_url(url: str) -> str:
    """Strip any userinfo from a URL before it is stored or logged."""
    parts = urlsplit(url)
    if "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[1]
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _extract_entries(payload: Any) -> list[dict[str, Any]] | None:
    """Accept a JSON array or ``{"entries": [...]}``; reject anything else."""
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        entries = payload.get("entries")
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
    return None


class NoneBackendLogCollector:
    """Collector that records backend logs as explicitly missing."""

    name = "backend_logs_none"

    def collect(self, incident: InvestigationInput) -> list[EvidenceItem]:
        return build_gap_evidence(
            "Backend log collection is disabled by configuration "
            "(BACKEND_LOGS_SOURCE=none). No logs were retrieved."
        )


def build_backend_log_collector(
    source: str | None = None,
) -> BackendLogCollector:
    """Return a collector for the configured backend logs source.

    Unconfigured or unknown sources record an explicit gap instead of
    guessing, so a report can never pretend logs existed.
    """
    configured = (source or settings.backend_logs_source).strip().lower()

    if configured == "local":
        return LocalInMemoryBackendLogCollector()
    if configured == "http":
        return HTTPBackendLogCollector()
    if configured == "gcp":
        # Imported lazily so the Google auth stack is only loaded when
        # the GCP source is actually selected (local/test imports stay
        # light, and the offline default never touches ADC).
        from app.services.gcp_logging import GoogleCloudLoggingCollector

        return GoogleCloudLoggingCollector()

    return NoneBackendLogCollector()


def normalize_logs_to_records(
    logs: Iterable[Any],
) -> list[BackendLogRecord]:
    """Normalize arbitrary log dicts into ``BackendLogRecord`` objects.

    Accepts common field spellings: message/msg, level/severity,
    timestamp/time/@timestamp (ISO 8601 string or datetime), service/app/
    name, request_id/requestId, trace_id/traceId, span_id/spanId,
    logger, metadata.
    """
    records: list[BackendLogRecord] = []

    for log in logs:
        if not isinstance(log, dict):
            continue

        message = log.get("message") or log.get("msg") or ""
        if not message:
            continue

        timestamp = log.get("timestamp") or log.get("time") or log.get("@timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp_dt = datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp_dt = None
        elif isinstance(timestamp, datetime):
            timestamp_dt = timestamp
        else:
            timestamp_dt = None

        metadata = log.get("metadata")
        records.append(
            BackendLogRecord(
                message=str(message),
                level=str(log.get("level") or log.get("severity") or ""),
                timestamp=timestamp_dt,
                service=log.get("service") or log.get("app") or log.get("name"),
                request_id=log.get("request_id") or log.get("requestId"),
                trace_id=log.get("trace_id") or log.get("traceId"),
                span_id=log.get("span_id") or log.get("spanId"),
                logger_name=log.get("logger"),
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        )

    return records
