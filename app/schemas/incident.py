import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "high", "medium", "low"]
IncidentStatus = Literal[
    "received",
    "queued",
    "investigating",
    "completed",
    "failed",
]

class IncidentCreate(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    source: Literal["grafana", "posthog", "manual"]
    severity: Severity
    service_name: str = Field(min_length=1, max_length=150)
    summary: str | None = None


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fingerprint: str
    title: str
    source: str
    severity: str
    status: str
    service_name: str
    summary: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime