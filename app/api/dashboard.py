"""Read-only dashboard endpoints: incidents with investigation state.

The dashboard needs two reads the incident API does not provide: an
incident list that includes investigation status and report
availability, and a detail view with the persisted report (root cause,
confidence, limitations, fixes, full evidence including explicit gaps).

Conventions are copied from the incident API on purpose:

* the same ``authenticate_incident_api`` HTTP Basic guard, so no new
  credential pair exists and the browser's native auth prompt works;
* the same paging parameters and 404 shape;
* read-only handlers - nothing here creates jobs, triggers
  investigations, or contacts any external service.

The only value transformed on the way out is the job's ``last_error``,
which passes through the shared redaction patterns so an operational
failure message can never leak a credential to the browser.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.incidents import authenticate_incident_api
from app.db.session import get_db
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.models.investigation_report import InvestigationReport
from app.schemas.dashboard import (
    DashboardIncident,
    DashboardIncidentDetail,
    DashboardInvestigationInfo,
    DashboardJobRead,
    DashboardReportRead,
)
from app.services.logs import redact_text

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["Dashboard"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]


def _latest_per_incident(rows):
    """Group rows (newest first) into the newest row per incident."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row.incident_id, row)
    return grouped


def _load_related(
    db: Session,
    incident_ids: list[uuid.UUID],
) -> tuple[dict, dict]:
    """Latest investigation job and latest report per incident.

    Two bounded queries regardless of list size (no per-row lookups).
    "Latest" is newest-first by timestamp with a deterministic
    tie-break, so equal timestamps cannot reorder rows between reads.
    """
    if not incident_ids:
        return {}, {}

    jobs = db.scalars(
        select(InvestigationJob)
        .where(InvestigationJob.incident_id.in_(incident_ids))
        .order_by(
            InvestigationJob.created_at.desc(),
            InvestigationJob.id.desc(),
        )
    ).all()

    reports = db.scalars(
        select(InvestigationReport)
        .where(InvestigationReport.incident_id.in_(incident_ids))
        .order_by(
            InvestigationReport.updated_at.desc(),
            InvestigationReport.id.desc(),
        )
    ).all()

    return _latest_per_incident(jobs), _latest_per_incident(reports)


def _investigation_info(
    job: InvestigationJob | None,
    report: InvestigationReport | None,
) -> DashboardInvestigationInfo:
    return DashboardInvestigationInfo(
        job_id=job.id if job is not None else None,
        job_status=job.status if job is not None else None,
        report_available=report is not None,
        report_status=report.status if report is not None else None,
    )


def _job_read(job: InvestigationJob) -> DashboardJobRead:
    payload = DashboardJobRead.model_validate(job)

    if payload.last_error:
        payload.last_error = redact_text(payload.last_error)

    return payload


@router.get(
    "/incidents",
    response_model=list[DashboardIncident],
    dependencies=[Depends(authenticate_incident_api)],
)
def list_dashboard_incidents(
    db: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Incidents (newest first) with investigation status per row."""
    incidents = list(
        db.scalars(
            select(Incident)
            .order_by(
                Incident.last_seen_at.desc(),
                Incident.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
    )

    jobs, reports = _load_related(db, [incident.id for incident in incidents])

    rows: list[DashboardIncident] = []
    for incident in incidents:
        row = DashboardIncident.model_validate(incident)
        row.investigation = _investigation_info(
            jobs.get(incident.id),
            reports.get(incident.id),
        )
        rows.append(row)

    return rows


@router.get(
    "/incidents/{incident_id}",
    response_model=DashboardIncidentDetail,
    dependencies=[Depends(authenticate_incident_api)],
)
def get_dashboard_incident(
    incident_id: uuid.UUID,
    db: DatabaseSession,
):
    """One incident with its latest job and persisted report.

    Authentication is enforced before existence (same as the incident
    API), so an unauthenticated caller cannot probe which IDs exist.
    """
    incident = db.get(Incident, incident_id)

    if incident is None:
        raise HTTPException(
            status_code=404,
            detail="Incident not found",
        )

    jobs, reports = _load_related(db, [incident.id])
    job = jobs.get(incident.id)
    report = reports.get(incident.id)

    detail = DashboardIncidentDetail.model_validate(incident)
    detail.investigation = _investigation_info(job, report)
    detail.job = _job_read(job) if job is not None else None
    detail.report = (
        DashboardReportRead.model_validate(report)
        if report is not None
        else None
    )

    return detail
