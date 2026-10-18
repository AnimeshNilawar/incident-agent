import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class InvestigationReport(Base):
    """The persisted result of one investigation job.

    One row per job (``job_id`` is unique): a repeated attempt for the
    same job replaces the previous row's content instead of adding a
    second report, so redelivered tasks can never duplicate reports.

    The structured fields (evidence, suggested fixes, limitations) are
    stored as JSON. That keeps the schema minimal - a report is a single
    immutable-by-convention document, not a queryable observability
    store - while still being fully typed in the application layer via
    ``app.schemas.investigation``.
    """

    __tablename__ = "investigation_reports"

    __table_args__ = (
        # One report per investigation job: a redelivered task replaces the
        # previous report instead of adding a second one.
        UniqueConstraint(
            "job_id",
            name="uq_investigation_reports_job_id",
        ),
        Index(
            "ix_investigation_reports_incident_id",
            "incident_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    # One report per investigation job. CASCADE keeps reports from
    # outliving the job they describe.
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Denormalized for dashboard queries (list a service's reports without
    # joining through jobs). CASCADE keeps it consistent with the job.
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # "completed" (a validated conclusion, possibly low-confidence) or
    # "inconclusive" (not enough evidence to state a root cause).
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    probable_root_cause: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    explanation: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 0.0 - 1.0. Nullable because an inconclusive investigation may have
    # no meaningful confidence to report.
    confidence: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    # Evidence IDs cited as supporting the root cause hypothesis.
    root_cause_evidence_ids: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # [{"title": ..., "description": ..., "evidence_ids": [...]}]
    suggested_fixes: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # Full evidence list as collected, including "missing" gap items.
    evidence: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    limitations: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # Which model produced the findings; null when no provider ran (for
    # example an investigation that found no evidence at all).
    model_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
