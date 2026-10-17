import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.incident import Incident
from app.schemas.incident import IncidentCreate, IncidentRead
from app.services.incidents import create_or_update_incident

router = APIRouter(
    prefix="/api/v1/incidents",
    tags=["Incidents"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]

# ``auto_error=False`` lets the dependency tell "no credentials" apart from
# "wrong credentials" and apply the same fail-closed logic as the Grafana
# webhook.
security = HTTPBasic(auto_error=False)


def authenticate_incident_api(
    credentials: Annotated[
        HTTPBasicCredentials | None,
        Depends(security),
    ],
) -> None:
    """Reject requests without valid configured incident API credentials.

    This mirrors the Grafana webhook guard (constant-time comparison, fail
    closed when unconfigured) but uses its own credential pair, so the webhook
    secret is never reusable as a general API credential. Values are compared
    as UTF-8 bytes so a non-ASCII credential is rejected as invalid rather
    than raising.
    """
    if not settings.incident_api_username or not settings.incident_api_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident API credentials are not configured",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incident API authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = hmac.compare_digest(
        credentials.username.encode("utf-8"),
        settings.incident_api_username.encode("utf-8"),
    )
    password_ok = hmac.compare_digest(
        credentials.password.encode("utf-8"),
        settings.incident_api_password.encode("utf-8"),
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid incident API credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


@router.post(
    "",
    response_model=IncidentRead,
    dependencies=[Depends(authenticate_incident_api)],
)
def create_incident(
    payload: IncidentCreate,
    db: DatabaseSession,
):
    incident, _created = create_or_update_incident(db, payload)
    return incident


@router.get(
    "",
    response_model=list[IncidentRead],
    dependencies=[Depends(authenticate_incident_api)],
)
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


@router.get(
    "/{incident_id}",
    response_model=IncidentRead,
    dependencies=[Depends(authenticate_incident_api)],
)
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
