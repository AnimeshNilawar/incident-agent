import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.incident import Incident
from app.schemas.incident import IncidentCreate, IncidentRead
from app.services.incidents import create_or_update_incident

router = APIRouter(
    prefix="/api/v1/incidents",
    tags=["Incidents"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]

@router.post("", response_model=IncidentRead)
def create_incident(
    payload: IncidentCreate,
    db: DatabaseSession,
):
    incident, _created = create_or_update_incident(db, payload)
    return incident

@router.get("", response_model=list[IncidentRead])
def list_incidents(
    db: DatabaseSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    statement = (
        select(Incident)
        .order_by(Incident.last_seen_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return list(db.scalars(statement).all())

@router.get("/{incident_id}", response_model=IncidentRead)
def get_incident(
    incident_id: uuid.UUID,
    db: DatabaseSession,
):
    incident = db.get(Incident, incident_id)

    if incident is None:
        raise HTTPException(
            status_code=404,
            detail="Incident not found",
        )

    return incident