"""Persistence for investigation reports.

One report row per investigation job: a repeated attempt (after a Cloud
Tasks redelivery, for example) replaces the previous row in place rather
than inserting a second report. The unique constraint on ``job_id`` is
the backstop if two writers ever race.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.investigation_report import InvestigationReport
from app.schemas.investigation import InvestigationReportData


def persist_investigation_report(
    db: Session,
    report: InvestigationReportData,
) -> tuple[InvestigationReport, bool]:
    """Insert or replace the report for this job; returns (row, created).

    Commits before returning, mirroring the other services: the caller
    (the worker) marks the job completed only after this succeeds, so a
    crash between the two commits leaves a report whose job is retried -
    and the retry replaces the report instead of duplicating it.
    """
    now = datetime.now(UTC)

    fields = {
        "incident_id": report.incident_id,
        "status": report.status,
        "probable_root_cause": report.probable_root_cause,
        "explanation": report.explanation,
        "confidence": report.confidence,
        "root_cause_evidence_ids": list(report.root_cause_evidence_ids),
        "suggested_fixes": [
            fix.model_dump() for fix in report.suggested_fixes
        ],
        "evidence": [
            item.model_dump(mode="json") for item in report.evidence
        ],
        "limitations": list(report.limitations),
        "model_name": report.model_name,
        "updated_at": now,
    }

    existing = db.scalar(
        select(InvestigationReport).where(
            InvestigationReport.job_id == report.job_id
        )
    )

    if existing is not None:
        for attribute, value in fields.items():
            setattr(existing, attribute, value)

        db.commit()
        db.refresh(existing)
        return existing, False

    row = InvestigationReport(
        job_id=report.job_id,
        created_at=now,
        **fields,
    )
    db.add(row)

    try:
        db.commit()
    except IntegrityError:
        # Another writer inserted the report for this job first; replace
        # their row instead of failing the investigation.
        db.rollback()

        existing = db.scalar(
            select(InvestigationReport).where(
                InvestigationReport.job_id == report.job_id
            )
        )

        if existing is None:
            raise

        for attribute, value in fields.items():
            setattr(existing, attribute, value)

        db.commit()
        db.refresh(existing)
        return existing, False

    db.refresh(row)
    return row, True
