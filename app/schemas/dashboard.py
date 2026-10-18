"""Response contracts for the read-only incident dashboard API.

The dashboard is a *viewer*: it exposes what the incident, job and
report models already store, under the same HTTP Basic guard as the
incident API. It never returns configuration or credentials - only
persisted incident data and already-collected (and at collection time
already-redacted) report content.

The shape keeps evidence and hypotheses separate so the UI can label
them distinctly:

* ``evidence`` items are observed facts - or explicit ``missing`` gaps
  that state which source was NOT collected.
* ``probable_root_cause``, ``explanation`` and ``suggested_fixes`` are
  AI-generated output. ``SuggestedFix`` is advisory by contract; nothing
  in this API implies a fix was applied.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.incident import IncidentRead
from app.schemas.investigation import EvidenceItem, SuggestedFix


class DashboardInvestigationInfo(BaseModel):
    """List-row view of an incident's investigation state.

    ``job_status`` is the latest job's lifecycle state (``pending`` ->
    ``queued`` -> ``running`` -> ``completed`` / ``failed``, or ``None``
    when no investigation was ever created for the incident).
    ``report_available``/``report_status`` say whether a validated
    report exists and whether its conclusion was ``completed`` or
    ``inconclusive``.
    """

    job_id: uuid.UUID | None = None
    job_status: str | None = None
    report_available: bool = False
    report_status: str | None = None


class DashboardIncident(IncidentRead):
    """One incident row with its investigation status."""

    investigation: DashboardInvestigationInfo = (
        DashboardInvestigationInfo()
    )


class DashboardJobRead(BaseModel):
    """The latest investigation job for an incident.

    ``last_error`` is operational diagnostics only; the API layer runs
    it through the shared redaction patterns before it is returned, so
    a failure message can never leak a credential to the browser.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class DashboardReportRead(BaseModel):
    """A persisted investigation report, exactly as stored.

    Evidence is re-validated through ``EvidenceItem`` (and fixes
    through ``SuggestedFix``) so the API contract matches the engine's
    contract - the dashboard cannot drift into rendering shape the
    engine never produced.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    incident_id: uuid.UUID
    # "completed" (states a probable root cause) or "inconclusive"
    # (states none - only limitations and low confidence).
    status: str
    probable_root_cause: str | None
    root_cause_evidence_ids: list[str]
    explanation: str | None
    confidence: float | None
    suggested_fixes: list[SuggestedFix]
    evidence: list[EvidenceItem]
    limitations: list[str]
    model_name: str | None
    created_at: datetime
    updated_at: datetime


class DashboardIncidentDetail(DashboardIncident):
    """Full detail view: incident + latest job + latest report.

    ``job``/``report`` are ``None`` when the incident has no
    investigation job or no report yet - the UI shows that as an
    explicit state rather than inventing content.
    """

    job: DashboardJobRead | None = None
    report: DashboardReportRead | None = None
