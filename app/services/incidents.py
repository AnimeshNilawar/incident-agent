from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.schemas.incident import IncidentCreate


def create_or_update_incident(
        db: Session,
        payload: IncidentCreate,
) -> tuple[Incident, bool]:
    """
    Create an incident or update an existing incident.

    Returns:
        (incident, created)
    """
    now = datetime.now(UTC)

    existing = db.scalar(
        select(Incident).where(
            Incident.fingerprint == payload.fingerprint
        )
    )

    if existing is not None:
        existing.last_seen_at = now
        existing.title = payload.title
        existing.severity = payload.severity
        existing.source = payload.source
        existing.service_name = payload.service_name

        if payload.summary is not None:
            existing.summary = payload.summary

        db.commit()
        db.refresh(existing)

        return existing, False

    incident = Incident(
        fingerprint=payload.fingerprint,
        title=payload.title,
        source=payload.source,
        severity=payload.severity,
        service_name=payload.service_name,
        summary=payload.summary,
        status="received",
        first_seen_at=now,
        last_seen_at=now,
    )

    db.add(incident)

    try:
        db.commit()
        db.refresh(incident)
        return incident, True

    except IntegrityError:
        # Another request may have inserted the same fingerprint.
        db.rollback()

        existing = db.scalar(
            select(Incident).where(
                Incident.fingerprint == payload.fingerprint
            )
        )

        if existing is None:
            raise

        existing.last_seen_at = now
        db.commit()
        db.refresh(existing)

        return existing, False
