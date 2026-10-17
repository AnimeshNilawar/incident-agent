
import hashlib
import hmac
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.investigation_job import InvestigationJob
from app.schemas.incident import IncidentCreate
from app.services.incidents import create_or_update_incident
from app.services.investigation_dispatcher import (
    dispatch_investigation_job,
)
from app.services.investigation_jobs import create_investigation_job

router = APIRouter(
    prefix="/api/v1/webhooks",
    tags=["Grafana Webhooks"],
)

security = HTTPBasic(auto_error=False)


def authenticate_grafana(
    credentials: Annotated[
        HTTPBasicCredentials | None,
        Depends(security),
    ],
) -> None:
    """Reject requests without valid configured webhook credentials."""
    if not settings.grafana_webhook_username or not settings.grafana_webhook_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Grafana webhook credentials are not configured",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = hmac.compare_digest(
        credentials.username,
        settings.grafana_webhook_username,
    )
    password_ok = hmac.compare_digest(
        credentials.password,
        settings.grafana_webhook_password,
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def normalize_severity(value: str | None) -> str:
    """Normalize Grafana severity labels to the incident API's values."""
    severity = (value or "").strip().lower()

    mapping = {
        "critical": "critical",
        "error": "critical",
        "high": "high",
        "warning": "medium",
        "warn": "medium",
        "medium": "medium",
        "info": "low",
        "informational": "low",
        "low": "low",
    }

    return mapping.get(severity, "medium")


def build_fingerprint(alert: dict[str, Any]) -> str:
    """Generate a stable identifier for repeated notifications of one alert."""
    labels = alert.get("labels") or {}
    grafana_fingerprint = alert.get("fingerprint")

    if grafana_fingerprint:
        return f"grafana:{grafana_fingerprint}"

    canonical_data = {
        "alertname": labels.get("alertname", "unknown-alert"),
        "labels": labels,
    }

    serialized = json.dumps(
        canonical_data,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    return f"grafana:{digest}"


def process_alert(
    alert: dict[str, Any],
    group_status: str,
    db: Session,
) -> str:
    """Create or update an incident from one Grafana alert."""
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}

    alert_state = str(
        alert.get("status") or group_status or "firing"
    ).lower()

    if alert_state not in {"firing", "resolved"}:
        raise ValueError(f"Unsupported alert state: {alert_state}")

    title = str(
        labels.get("alertname")
        or annotations.get("summary")
        or "Unnamed Grafana alert"
    )

    service_name = (
        labels.get("service_name")
        or labels.get("service")
        or labels.get("job")
        or labels.get("app")
        or "unknown"
    )

    summary_parts = []

    if annotations.get("summary"):
        summary_parts.append(str(annotations["summary"]))

    if annotations.get("description"):
        summary_parts.append(str(annotations["description"]))

    summary_parts.append(f"Grafana alert state: {alert_state}")

    incident_data = IncidentCreate(
        fingerprint=build_fingerprint(alert),
        title=title,
        source="grafana",
        severity=normalize_severity(labels.get("severity")),
        service_name=str(service_name),
        summary="\n\n".join(summary_parts),
    )

    
    
    incident, created = create_or_update_incident(db, incident_data)

    was_completed = incident.status == "completed"

    # Create a job for a new or reopened firing incident.
    if alert_state == "firing" and (created or was_completed):
        create_investigation_job(db, incident)

    if alert_state == "resolved":
        incident.status = "completed"
    elif alert_state == "firing" and was_completed:
        incident.status = "received"

    # Persist the incident and job before contacting Cloud Tasks.
    db.commit()
    db.refresh(incident)

    if alert_state == "firing":
        # A previous queue attempt may have failed. Retry pending work
        # when Grafana sends another notification for this incident.
        pending_job = db.scalar(
            select(InvestigationJob)
            .where(
                InvestigationJob.incident_id == incident.id,
                InvestigationJob.status == "pending",
            )
            .order_by(InvestigationJob.created_at.desc())
            .limit(1)
        )

        if pending_job is not None:
            dispatch_investigation_job(db, pending_job)

    if alert_state == "resolved":
        return "resolved"

    return "created" if created else "updated"


@router.post(
    "/grafana",
    dependencies=[Depends(authenticate_grafana)],
)
def receive_grafana_webhook(
    payload: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, int | str]:
    """Receive Grafana's standard webhook notification payload."""
    alerts = payload.get("alerts")

    if not isinstance(alerts, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must contain an alerts array",
        )

    group_status = str(payload.get("status") or "firing").lower()

    if group_status not in {"firing", "resolved"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported alert group status",
        )

    counts = {
        "created": 0,
        "updated": 0,
        "resolved": 0,
    }

    try:
        for alert in alerts:
            if not isinstance(alert, dict):
                raise TypeError("Each alert must be an object")

            result = process_alert(alert, group_status, db)

            if result == "created":
                counts["created"] += 1
            elif result == "updated":
                counts["updated"] += 1
            else:
                counts["resolved"] += 1

    except (ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception:
        db.rollback()
        raise

    return {
        "status": "accepted",
        "received": len(alerts),
        **counts,
    }
