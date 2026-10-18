"""The investigation engine: evidence in, structured report out.

Flow for one claimed job:

1. Build an ``InvestigationInput`` from the already-persisted incident.
2. Collect evidence (alert payload, backend logs and frontend errors
   from PostHog; further collectors such as source-code context can be
   added without changing this module).
3. Ask the LLM provider for findings as JSON and strictly validate them:
   anything malformed, out of range, or citing evidence we never
   collected fails the attempt instead of being stored.
4. Persist the validated report. Only after persistence succeeds does
   the worker mark the job completed.

The engine never modifies source code and never executes suggested
fixes; every output is advisory and stored for a human to review.
"""

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.models.investigation_report import InvestigationReport
from app.schemas.investigation import (
    EvidenceItem,
    InvestigationInput,
    InvestigationReportData,
    LLMFindings,
)
from app.services.evidence import (
    AlertPayloadEvidenceCollector,
    EvidenceCollector,
    collect_evidence,
)
from app.services.investigation_reports import persist_investigation_report
from app.services.llm import build_default_provider
from app.services.logs import build_backend_log_collector
from app.services.posthog_errors import PostHogErrorEvidenceCollector

logger = logging.getLogger(__name__)

# How much of the model's raw output to echo into a validation error.
# Enough to debug a bad response; bounded so logs stay small.
MAX_RAW_OUTPUT_SNIPPET_CHARS = 300

SYSTEM_PROMPT = """\
You are an incident investigation assistant for the Micuppa platform.
You will receive a JSON object with two keys: "incident" (the alert that
triggered the investigation) and "evidence" (a list of evidence items).

Evidence items have a "status":
- "available" items contain observed material you may analyze.
- "missing" items record sources that were NOT collected. Treat those
  sources as unavailable: you may mention the gap in "limitations", but
  you must never invent their contents.

Strict rules:
1. Base every statement only on the provided evidence. Never invent
   logs, stack traces, metric values, code lines, or timelines.
   "backend-logs" and "posthog-errors" content is observed fact: cite
   it as-is and keep observations ("logs show X") separate from your
   hypothesis about causation ("X likely happened because Y").
2. Every causal claim in "probable_root_cause" and every entry in
   "suggested_fixes" must cite supporting evidence via their
   "*_evidence_ids" arrays, using only ids from the provided evidence.
3. If the evidence does not support a probable root cause - for
   example the only items are alert attributes and "missing" gaps, or
   the logs are inconclusive - set "status" to "inconclusive",
   "probable_root_cause" to null, "root_cause_evidence_ids" to [], and
   explain what is missing in "limitations". Give an honest low
   "confidence".
4. "confidence" is a number between 0.0 and 1.0, meaning: 1.0 only when
   the evidence directly demonstrates the cause; below 0.5 when you are
   speculating.
5. "suggested_fixes" are recommendations for a human operator. Describe
   them; never state that a change was or should be applied
   automatically.

Respond with exactly one JSON object (no prose, no markdown fences):
{
  "status": "completed" | "inconclusive",
  "probable_root_cause": string | null,
  "root_cause_evidence_ids": [string, ...],
  "explanation": string,
  "confidence": number between 0.0 and 1.0,
  "suggested_fixes": [
    {"title": string, "description": string, "evidence_ids": [string, ...]}
  ],
  "limitations": [string, ...]
}"""


class InvalidLLMOutputError(RuntimeError):
    """The model's response could not be validated against the contract."""


def build_investigation_input(
    job: InvestigationJob,
    incident: Incident,
) -> InvestigationInput:
    """Project the persisted rows into the engine's input contract."""
    return InvestigationInput(
        incident_id=incident.id,
        job_id=job.id,
        fingerprint=incident.fingerprint,
        title=incident.title,
        source=incident.source,
        severity=incident.severity,
        service_name=incident.service_name,
        summary=incident.summary,
        first_seen_at=incident.first_seen_at,
        last_seen_at=incident.last_seen_at,
    )


def build_user_prompt(
    incident: InvestigationInput,
    evidence: list[EvidenceItem],
) -> str:
    """Serialize incident + evidence for the model."""
    payload = {
        "incident": incident.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _invalid_output(reason: str, raw: str) -> InvalidLLMOutputError:
    snippet = raw.strip()[:MAX_RAW_OUTPUT_SNIPPET_CHARS]
    return InvalidLLMOutputError(f"{reason} (output starts with: {snippet!r})")


def parse_findings(
    raw: str,
    evidence: list[EvidenceItem],
) -> LLMFindings:
    """Parse and strictly validate the model's raw output.

    Rejects malformed JSON, schema violations, and - critically -
    citations of evidence ids that were never collected, so a report can
    never reference fabricated material.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _invalid_output(
            f"LLM output is not valid JSON ({exc.msg})", raw
        ) from exc

    try:
        findings = LLMFindings.model_validate(payload)
    except ValidationError as exc:
        raise _invalid_output(
            f"LLM output failed schema validation: {exc.errors()[0]['msg']}",
            raw,
        ) from exc

    known_ids = {item.id for item in evidence}

    cited_ids = list(
        dict.fromkeys(
            [*findings.root_cause_evidence_ids]
            + [
                evidence_id
                for fix in findings.suggested_fixes
                for evidence_id in fix.evidence_ids
            ]
        )
    )
    unknown_ids = [cited for cited in cited_ids if cited not in known_ids]

    if unknown_ids:
        raise InvalidLLMOutputError(
            "LLM output cites evidence that was never collected: "
            f"{', '.join(sorted(unknown_ids))}"
        )

    if findings.status == "completed" and not findings.root_cause_evidence_ids:
        raise InvalidLLMOutputError(
            "LLM output states a root cause without citing any evidence"
        )

    return findings


def build_report_data(
    incident: InvestigationInput,
    evidence: list[EvidenceItem],
    findings: LLMFindings,
    model_name: str | None,
) -> InvestigationReportData:
    """Combine validated findings with engine-owned identifiers."""
    return InvestigationReportData(
        job_id=incident.job_id,
        incident_id=incident.incident_id,
        status=findings.status,
        probable_root_cause=findings.probable_root_cause,
        root_cause_evidence_ids=list(findings.root_cause_evidence_ids),
        explanation=findings.explanation,
        confidence=findings.confidence,
        suggested_fixes=findings.suggested_fixes,
        evidence=evidence,
        limitations=list(findings.limitations),
        model_name=model_name,
    )


def _no_evidence_findings() -> LLMFindings:
    """Honest outcome when nothing at all could be collected.

    No model is consulted: there would be nothing to ground it on, and a
    model asked to speculate without data produces exactly the kind of
    confident fiction this engine exists to avoid.
    """
    return LLMFindings(
        status="inconclusive",
        probable_root_cause=None,
        root_cause_evidence_ids=[],
        explanation=(
            "No evidence was available to analyze, so no root cause "
            "could be determined."
        ),
        confidence=0.0,
        suggested_fixes=[],
        limitations=[
            (
                "No evidence sources were available for this incident; "
                "no model analysis was performed."
            )
        ],
    )


def run_investigation(
    db: Session,
    *,
    job: InvestigationJob,
    incident: Incident,
    provider=None,
    collectors: tuple[EvidenceCollector, ...] | None = None,
) -> InvestigationReport:
    """Investigate one incident and persist the report; returns the row.

    Raises on any failure (provider errors, invalid model output,
    persistence errors) so the caller's existing job-failure path
    records the error and lets Cloud Tasks retry within the job's
    attempt limit.
    """
    incident_input = build_investigation_input(job, incident)

    if collectors is None:
        # Combine base alert evidence with backend log and frontend
        # error evidence. Both collectors return an explicit gap when
        # their source is not configured or has nothing relevant, so
        # the engine never invents evidence and an unconfigured source
        # appears on the report as a recorded limitation.
        log_collector = build_backend_log_collector()
        collectors = (
            AlertPayloadEvidenceCollector(),
            log_collector,
            PostHogErrorEvidenceCollector(),
        )

    evidence = collect_evidence(incident_input, collectors=collectors)

    available_evidence = [
        item
        for item in evidence
        if item.status == "available" and item.content
    ]

    if not available_evidence:
        findings = _no_evidence_findings()
        model_name = None
    else:
        if provider is None:
            provider = build_default_provider()

        raw = provider.complete_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(incident_input, evidence),
        )
        findings = parse_findings(raw, evidence)
        model_name = getattr(provider, "model", None)

    report_data = build_report_data(
        incident_input,
        evidence,
        findings,
        model_name,
    )

    row, created = persist_investigation_report(db, report_data)

    logger.info(
        "Investigation %s for job %s (incident %s): status=%s "
        "confidence=%s model=%s",
        "created" if created else "replaced",
        job.id,
        incident.id,
        row.status,
        row.confidence,
        row.model_name,
    )

    return row
