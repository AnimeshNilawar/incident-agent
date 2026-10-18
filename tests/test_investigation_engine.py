"""Tests for the investigation engine and report persistence.

Everything here is offline and deterministic: the "LLM" is a fake
provider returning canned JSON, so no test ever contacts a real model
or any hosted service.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  # Registers tables on Base.metadata
from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.models.investigation_report import InvestigationReport
from app.schemas.investigation import (
    EvidenceItem,
    InvestigationReportData,
    LLMFindings,
)
from app.services.investigation_engine import (
    InvalidLLMOutputError,
    build_investigation_input,
    build_user_prompt,
    parse_findings,
    run_investigation,
)
from app.services.investigation_reports import persist_investigation_report

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_incident(db, summary="CPU above threshold for 10 minutes"):
    incident = Incident(
        fingerprint=f"engine-test-{uuid.uuid4()}",
        title="BackendHighCPU",
        source="grafana",
        severity="high",
        status="received",
        service_name="micuppa-backend-india",
        summary=summary,
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    db.add(incident)
    db.flush()

    job = InvestigationJob(
        incident_id=incident.id,
        idempotency_key=f"engine-job:{uuid.uuid4()}",
        # A fresh, claimable job. Tests that exercise the claim path use
        # claim_job; the rest treat the job as already claimed.
        status="queued",
        attempts=0,
        max_attempts=3,
    )
    db.add(job)
    db.commit()

    return incident, job


class FakeProvider:
    """Deterministic stand-in for the LLM. Records calls; returns canned JSON."""

    model = "fake-model"

    def __init__(self, output: dict | str | None = None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls: list[dict] = []

    def complete_json(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self.error is not None:
            raise self.error
        if isinstance(self.output, str):
            return self.output
        return json.dumps(self.output)


def valid_findings(**overrides) -> dict:
    findings = {
        "status": "completed",
        "probable_root_cause": (
            "A recent deployment (evidence not available) is the most "
            "likely cause based on the alert timing."
        ),
        "root_cause_evidence_ids": ["alert-summary"],
        "explanation": (
            "The alert summary reports sustained CPU above threshold; "
            "no further evidence was available to narrow it down."
        ),
        "confidence": 0.55,
        "suggested_fixes": [
            {
                "title": "Roll back the most recent backend deployment",
                "description": (
                    "Compare against the previous release; if the alert "
                    "began at deploy time, roll back and observe CPU."
                ),
                "evidence_ids": ["alert-summary", "alert-metadata"],
            }
        ],
        "limitations": [
            "Backend logs were not collected",
            "Source code could not be inspected",
        ],
    }
    findings.update(overrides)
    return findings


def report_data_for(job, incident, **overrides) -> InvestigationReportData:
    fields = {
        "job_id": job.id,
        "incident_id": incident.id,
        "status": "inconclusive",
        "probable_root_cause": None,
        "root_cause_evidence_ids": [],
        "explanation": "Not enough evidence.",
        "confidence": 0.1,
        "suggested_fixes": [],
        "evidence": [
            EvidenceItem(
                id="alert-summary",
                source="grafana_alert",
                description="Alert summary.",
                status="missing",
            )
        ],
        "limitations": ["No evidence collected"],
        "model_name": None,
    }
    fields.update(overrides)
    return InvestigationReportData(**fields)


# --------------------------------------------------------------------------
# Contract / prompt construction
# --------------------------------------------------------------------------


def test_build_investigation_input_projects_incident_fields(db_session):
    incident, job = make_incident(db_session)

    model = build_investigation_input(job, incident)

    assert model.incident_id == incident.id
    assert model.job_id == job.id
    assert model.title == "BackendHighCPU"
    assert model.service_name == "micuppa-backend-india"
    assert model.summary == incident.summary
    assert model.alert_metadata == {}


def test_user_prompt_contains_incident_and_evidence_json(db_session):
    incident, job = make_incident(db_session)
    model = build_investigation_input(job, incident)

    from app.services.evidence import collect_evidence

    evidence = collect_evidence(model)
    prompt = build_user_prompt(model, evidence)

    payload = json.loads(prompt)
    assert payload["incident"]["title"] == "BackendHighCPU"
    evidence_ids = {item["id"] for item in payload["evidence"]}
    assert {"alert-summary", "alert-metadata"} <= evidence_ids
    # Explicit gaps are always recorded.
    assert "gap-backend-logs" in evidence_ids


# --------------------------------------------------------------------------
# Finding validation
# --------------------------------------------------------------------------


def _sample_evidence():
    return [
        EvidenceItem(
            id="alert-summary",
            source="grafana_alert",
            description="Summary.",
            status="available",
            content="CPU high",
        ),
        EvidenceItem(
            id="alert-metadata",
            source="grafana_alert",
            description="Metadata.",
            status="available",
            content="service: micuppa-backend-india",
        ),
        EvidenceItem(
            id="gap-backend-logs",
            source="backend_logs",
            description="Not collected.",
            status="missing",
        ),
    ]


def test_parse_findings_accepts_valid_output():
    findings = parse_findings(json.dumps(valid_findings()), _sample_evidence())
    assert findings.status == "completed"
    assert findings.confidence == 0.55
    assert findings.root_cause_evidence_ids == ["alert-summary"]


def test_parse_findings_rejects_non_json():
    with pytest.raises(InvalidLLMOutputError, match="not valid JSON"):
        parse_findings("sure, the problem is the database", _sample_evidence())


def test_parse_findings_rejects_schema_violation():
    bad = valid_findings(confidence=1.5)  # out of range
    with pytest.raises(InvalidLLMOutputError, match="schema validation"):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_missing_required_field():
    bad = valid_findings()
    del bad["explanation"]
    with pytest.raises(InvalidLLMOutputError, match="schema validation"):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_citation_of_uncollected_evidence():
    bad = valid_findings(root_cause_evidence_ids=["nonexistent-log-id"])
    with pytest.raises(InvalidLLMOutputError, match="never collected"):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_fix_citing_uncollected_evidence():
    bad = valid_findings(
        suggested_fixes=[
            {
                "title": "Fix it",
                "description": "Apply the obvious fix.",
                "evidence_ids": ["made-up-stack-trace"],
            }
        ]
    )
    with pytest.raises(InvalidLLMOutputError, match="never collected"):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_completed_without_root_cause():
    bad = valid_findings(probable_root_cause=None, root_cause_evidence_ids=[])
    with pytest.raises(InvalidLLMOutputError):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_completed_without_citations():
    bad = valid_findings(root_cause_evidence_ids=[])
    with pytest.raises(
        InvalidLLMOutputError, match="without citing any evidence"
    ):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_inconclusive_with_root_cause():
    bad = valid_findings(
        status="inconclusive",
        probable_root_cause="Speculative cause",
        root_cause_evidence_ids=["alert-summary"],
    )
    with pytest.raises(InvalidLLMOutputError, match="schema validation"):
        parse_findings(json.dumps(bad), _sample_evidence())


def test_parse_findings_rejects_non_object_json():
    with pytest.raises(InvalidLLMOutputError, match="schema validation"):
        parse_findings(json.dumps(["a", "list"]), _sample_evidence())


# --------------------------------------------------------------------------
# Engine run: valid generation + persistence
# --------------------------------------------------------------------------


def test_run_investigation_persists_valid_report(db_session):
    incident, job = make_incident(db_session)
    provider = FakeProvider(valid_findings())

    row = run_investigation(
        db_session, job=job, incident=incident, provider=provider
    )

    assert row.status == "completed"
    assert row.probable_root_cause is not None
    assert row.confidence == 0.55
    assert row.root_cause_evidence_ids == ["alert-summary"]
    assert row.model_name == "fake-model"
    assert row.job_id == job.id
    assert row.incident_id == incident.id

    # Full evidence (including explicit gaps) is persisted with the report.
    evidence_ids = {item["id"] for item in row.evidence}
    assert {"alert-summary", "alert-metadata"} <= evidence_ids
    assert "gap-backend-logs" in evidence_ids

    # Suggested fixes survive as structured data.
    assert row.suggested_fixes[0]["title"].startswith("Roll back")
    assert len(provider.calls) == 1


def test_report_is_retrievable_from_a_fresh_session(db_session):
    incident, job = make_incident(db_session)
    incident_id, job_id = incident.id, job.id

    run_investigation(
        db_session,
        job=job,
        incident=incident,
        provider=FakeProvider(valid_findings()),
    )

    # A brand-new session bound to the same engine (not the one that
    # wrote the row) must read the report back with parsed JSON fields.
    fresh_factory = sessionmaker(
        bind=db_session.get_bind(),
        autoflush=False,
        expire_on_commit=False,
    )

    with fresh_factory() as fresh:
        row = fresh.scalar(
            select(InvestigationReport).where(
                InvestigationReport.job_id == job_id
            )
        )
        assert row is not None
        assert row.incident_id == incident_id
        assert row.status == "completed"
        assert isinstance(row.evidence, list)
        assert isinstance(row.suggested_fixes, list)


def test_inconclusive_report_has_no_root_cause_and_keeps_limitations(
    db_session,
):
    incident, job = make_incident(db_session)
    provider = FakeProvider(
        valid_findings(
            status="inconclusive",
            probable_root_cause=None,
            root_cause_evidence_ids=[],
            confidence=0.2,
            explanation="The available evidence is insufficient.",
            suggested_fixes=[],
            limitations=[
                "Only the alert payload was available",
                "No logs, metrics, or code were collected",
            ],
        )
    )

    row = run_investigation(
        db_session, job=job, incident=incident, provider=provider
    )

    assert row.status == "inconclusive"
    assert row.probable_root_cause is None
    assert row.root_cause_evidence_ids == []
    assert row.confidence == 0.2
    assert row.suggested_fixes == []
    assert "No logs, metrics, or code were collected" in row.limitations


# --------------------------------------------------------------------------
# Engine run: provider failures and invalid output
# --------------------------------------------------------------------------


def test_provider_error_propagates_and_persists_nothing(db_session):
    from app.services.llm import LLMTransientError

    incident, job = make_incident(db_session)
    provider = FakeProvider(error=LLMTransientError("LLM timed out"))

    with pytest.raises(LLMTransientError, match="timed out"):
        run_investigation(
            db_session, job=job, incident=incident, provider=provider
        )

    assert db_session.scalar(select(InvestigationReport)) is None


def test_invalid_output_propagates_and_persists_nothing(db_session):
    incident, job = make_incident(db_session)
    provider = FakeProvider("this is not json at all")

    with pytest.raises(InvalidLLMOutputError):
        run_investigation(
            db_session, job=job, incident=incident, provider=provider
        )

    assert db_session.scalar(select(InvestigationReport)) is None


def test_unconfigured_provider_is_reported_explicitly(db_session, monkeypatch):
    from app.services.llm import LLMConfigurationError

    incident, job = make_incident(db_session)

    def unconfigured() -> None:
        raise LLMConfigurationError("LLM provider is not configured")

    # With no provider passed in and none configurable, the engine must
    # raise the configuration error rather than fabricate a report.
    monkeypatch.setattr(
        "app.services.investigation_engine.build_default_provider",
        unconfigured,
    )

    with pytest.raises(LLMConfigurationError, match="not configured"):
        run_investigation(db_session, job=job, incident=incident)

    # Nothing was persisted for the failed investigation.
    assert db_session.scalar(select(InvestigationReport)) is None


# --------------------------------------------------------------------------
# Idempotency across repeated attempts
# --------------------------------------------------------------------------


def test_repeated_attempts_replace_report_instead_of_duplicating(db_session):
    incident, job = make_incident(db_session)

    first = run_investigation(
        db_session,
        job=job,
        incident=incident,
        provider=FakeProvider(valid_findings(confidence=0.4)),
    )
    second = run_investigation(
        db_session,
        job=job,
        incident=incident,
        provider=FakeProvider(valid_findings(confidence=0.9)),
    )

    rows = db_session.scalars(
        select(InvestigationReport).where(
            InvestigationReport.job_id == job.id
        )
    ).all()

    assert len(rows) == 1
    assert first.id == second.id
    assert rows[0].confidence == 0.9


def test_unique_constraint_prevents_two_reports_for_one_job(db_session):
    incident, job = make_incident(db_session)
    report = report_data_for(job, incident)

    persist_investigation_report(db_session, report)

    # A second insert for the same job_id must violate the unique
    # constraint (the service normally replaces, this checks the DB-level
    # backstop by inserting directly).
    duplicate = InvestigationReport(
        job_id=job.id,
        incident_id=incident.id,
        status="completed",
        root_cause_evidence_ids=[],
        suggested_fixes=[],
        evidence=[],
        limitations=[],
    )
    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


# --------------------------------------------------------------------------
# Missing evidence / no available evidence at all
# --------------------------------------------------------------------------


def test_no_available_evidence_skips_llm_and_is_inconclusive(db_session):
    incident, job = make_incident(db_session, summary=None)

    class NoEvidenceCollectors:
        name = "none"

        def collect(self, incident_input):
            return [
                EvidenceItem(
                    id="gap-only",
                    source="backend_logs",
                    description="Nothing collected.",
                    status="missing",
                )
            ]

    provider = FakeProvider(valid_findings())

    row = run_investigation(
        db_session,
        job=job,
        incident=incident,
        provider=provider,
        collectors=(NoEvidenceCollectors(),),
    )

    # No evidence -> no model call, honest inconclusive report.
    assert provider.calls == []
    assert row.status == "inconclusive"
    assert row.probable_root_cause is None
    assert row.model_name is None
    assert any("No evidence" in limitation for limitation in row.limitations)


def test_duplicate_evidence_ids_are_rejected(db_session):
    incident, job = make_incident(db_session)
    incident_input = build_investigation_input(job, incident)

    class CollidingCollector:
        name = "colliding"

        def collect(self, incident_input):
            item = EvidenceItem(
                id="alert-summary",
                source="grafana_alert",
                description="Duplicate id.",
                status="available",
                content="x",
            )
            return [item, item]

    from app.services.evidence import collect_evidence

    with pytest.raises(ValueError, match="duplicate evidence id"):
        collect_evidence(
            incident_input, collectors=(CollidingCollector(),)
        )


# --------------------------------------------------------------------------
# Persistence service details
# --------------------------------------------------------------------------


def test_persist_reports_created_then_replaced(db_session):
    incident, job = make_incident(db_session)

    row, created = persist_investigation_report(
        db_session, report_data_for(job, incident, confidence=0.3)
    )
    assert created is True

    row2, created2 = persist_investigation_report(
        db_session, report_data_for(job, incident, confidence=0.8)
    )
    assert created2 is False
    assert row2.id == row.id
    assert row2.confidence == 0.8


def test_persist_serializes_evidence_and_fixes_as_json(db_session):
    incident, job = make_incident(db_session)

    fixes = [
        {
            "title": "Roll back the most recent backend deployment",
            "description": (
                "Compare against the previous release; if the alert "
                "began at deploy time, roll back and observe CPU."
            ),
            "evidence_ids": ["alert-summary", "alert-metadata"],
        }
    ]
    findings = LLMFindings.model_validate(valid_findings())
    data = InvestigationReportData(
        job_id=job.id,
        incident_id=incident.id,
        status=findings.status,
        probable_root_cause=findings.probable_root_cause,
        root_cause_evidence_ids=findings.root_cause_evidence_ids,
        explanation=findings.explanation,
        confidence=findings.confidence,
        suggested_fixes=findings.suggested_fixes,
        evidence=[
            EvidenceItem(
                id="alert-summary",
                source="grafana_alert",
                description="Summary.",
                status="available",
                content="CPU high",
                observed_at=datetime.now(UTC),
            )
        ],
        limitations=findings.limitations,
        model_name="fake-model",
    )

    row, _ = persist_investigation_report(db_session, data)

    assert row.suggested_fixes == fixes
    assert row.evidence[0]["id"] == "alert-summary"
    assert isinstance(row.evidence[0]["observed_at"], str)
    assert isinstance(row.limitations, list)


# --------------------------------------------------------------------------
# Worker lifecycle integration (engine failure -> job failure -> retry)
# --------------------------------------------------------------------------


def test_engine_failure_fails_job_and_allows_retry(db_session):
    from app.services.job_lifecycle import (
        ClaimOutcome,
        claim_job,
        complete_job,
        fail_job,
    )
    from app.services.llm import LLMTransientError

    incident, job = make_incident(db_session)

    claim = claim_job(
        db_session, job_id=job.id, incident_id=incident.id
    )
    assert claim.outcome == ClaimOutcome.CLAIMED

    provider = FakeProvider(error=LLMTransientError("provider down"))
    with pytest.raises(LLMTransientError):
        run_investigation(
            db_session, job=job, incident=incident, provider=provider
        )

    db_session.rollback()

    assert fail_job(
        db_session,
        job_id=job.id,
        claim_token=claim.claim_token,
        error="provider down",
    )
    db_session.expire_all()
    failed = db_session.get(InvestigationJob, job.id)
    assert failed.status == "failed"
    assert failed.last_error == "provider down"

    # A redelivery can reclaim the failed job and succeed.
    retry = claim_job(db_session, job_id=job.id, incident_id=incident.id)
    assert retry.outcome == ClaimOutcome.CLAIMED

    row = run_investigation(
        db_session,
        job=job,
        incident=incident,
        provider=FakeProvider(valid_findings()),
    )
    assert row.status == "completed"

    assert complete_job(
        db_session, job_id=job.id, claim_token=retry.claim_token
    )
    db_session.expire_all()
    assert db_session.get(InvestigationJob, job.id).status == "completed"

    # Exactly one report row exists after failure + successful retry.
    rows = db_session.scalars(
        select(InvestigationReport).where(
            InvestigationReport.job_id == job.id
        )
    ).all()
    assert len(rows) == 1


def test_persistence_failure_is_a_hard_error(db_session, monkeypatch):
    incident, job = make_incident(db_session)

    def broken_persist(*args, **kwargs):
        raise SQLAlchemyError("database write failed")

    monkeypatch.setattr(
        "app.services.investigation_engine.persist_investigation_report",
        broken_persist,
    )

    with pytest.raises(SQLAlchemyError, match="database write failed"):
        run_investigation(
            db_session,
            job=job,
            incident=incident,
            provider=FakeProvider(valid_findings()),
        )
