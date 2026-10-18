"""Typed contracts for the investigation pipeline.

The engine consumes an ``InvestigationInput`` (what we know about the
incident), gathers ``EvidenceItem`` records (facts, explicitly marked when
a source is unavailable), asks the LLM for findings, and persists an
``InvestigationReportData``.

Evidence and hypotheses are strictly separated: an ``EvidenceItem`` only
ever describes something observed (or explicitly *not* observable), while
the causal claims live in the report itself and must cite evidence IDs.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Investigation status stored on the report.
# * ``completed``  - the engine ran and produced a validated conclusion
#   (the conclusion itself may still be low-confidence; ``confidence`` and
#   ``limitations`` express that honestly).
# * ``inconclusive`` - there was not enough evidence to state a probable
#   root cause at all.
InvestigationStatus = Literal["completed", "inconclusive"]

# Whether an evidence item was actually collected or is a recorded gap.
# ``missing`` items are first-class: they tell the LLM and the dashboard
# which sources were unavailable, so a report can never silently pretend
# a log source existed.
EvidenceStatus = Literal["available", "missing"]


class InvestigationInput(BaseModel):
    """Everything the engine knows about one incident when it starts.

    Built from the persisted ``Incident`` row. ``alert_metadata`` carries
    any extra key/value context that arrived with the triggering alert;
    it is kept separate from the narrative ``summary`` so the prompt can
    present both.
    """

    incident_id: uuid.UUID
    job_id: uuid.UUID
    fingerprint: str
    title: str
    source: str
    severity: str
    service_name: str
    summary: str | None = None
    alert_metadata: dict[str, str] = Field(default_factory=dict)
    first_seen_at: datetime
    last_seen_at: datetime


class EvidenceItem(BaseModel):
    """One piece of evidence. ``id`` is stable so the report can cite it.

    ``content`` holds the observed material (an alert annotation, later a
    log excerpt). For ``missing`` items it stays ``None`` and
    ``description`` explains what could not be collected and why.
    """

    id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)
    status: EvidenceStatus = "available"
    content: str | None = None
    observed_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class SuggestedFix(BaseModel):
    """A recommended remediation. Advisory only - never auto-applied."""

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    # Evidence IDs this fix is based on. Validated against collected
    # evidence before persistence so a fix can never cite fabricated data.
    evidence_ids: list[str] = Field(default_factory=list)


def _validate_status_and_root_cause(
    status: InvestigationStatus,
    probable_root_cause: str | None,
) -> None:
    """Keep status and the root-cause hypothesis consistent.

    * ``completed`` must state a probable root cause - a "conclusion"
      with none would be indistinguishable from a guess.
    * ``inconclusive`` must not assert a root cause; its honest output
      is the limitations list and a low confidence value.
    """
    if status == "completed" and not probable_root_cause:
        raise ValueError(
            "a completed investigation must state a probable root cause"
        )

    if status == "inconclusive" and probable_root_cause:
        raise ValueError(
            "an inconclusive investigation must not state a root cause"
        )


class LLMFindings(BaseModel):
    """The structured output the LLM must return.

    Deliberately excludes job and incident IDs: those are attached by the
    engine from the already-claimed job row and are never trusted from
    model output. ``root_cause_evidence_ids`` are validated against the
    actually-collected evidence before anything is persisted, so a model
    hallucinating an evidence ID fails the attempt instead of producing a
    report that cites data we never had.
    """

    status: InvestigationStatus
    probable_root_cause: str | None = None
    root_cause_evidence_ids: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_fixes: list[SuggestedFix] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def _check_status_root_cause(self) -> "LLMFindings":
        _validate_status_and_root_cause(self.status, self.probable_root_cause)
        return self


class InvestigationReportData(BaseModel):
    """The validated outcome of one investigation, ready to persist.

    ``probable_root_cause`` is a *hypothesis* - it is only non-null when
    ``status`` is ``completed`` and it must cite at least one evidence ID
    via ``root_cause_evidence_ids``. ``confidence`` (0-1) and
    ``limitations`` state honestly how firm the conclusion is and what
    evidence was missing.
    """

    job_id: uuid.UUID
    incident_id: uuid.UUID
    status: InvestigationStatus
    probable_root_cause: str | None = None
    root_cause_evidence_ids: list[str] = Field(default_factory=list)
    explanation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_fixes: list[SuggestedFix] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # Name of the model that produced the findings, for traceability.
    model_name: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def _validate_status_and_root_cause(self) -> "InvestigationReportData":
        _validate_status_and_root_cause(self.status, self.probable_root_cause)
        return self
