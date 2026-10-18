"""add investigation reports

Adds ``incident_agent.investigation_reports``: one report row per
investigation job, holding the structured outcome of the LLM
investigation (root-cause hypothesis, explanation, confidence, evidence
including explicitly-missing sources, suggested fixes, limitations).

The revision ID is fixed and the schema name is duplicated from
``app.config.DEFAULT_DATABASE_SCHEMA`` for the same reason as in the
initial migration: a migration file must stay reproducible and
independent of runtime settings.

Revision ID: c8a1f3d92b47
Revises: 794b420b8bfc
Create Date: 2026-10-18 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, alembic autogenerate
revision: str = "c8a1f3d92b47"
down_revision: Union[str, Sequence[str], None] = "794b420b8bfc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed on purpose: see the initial migration (794b420b8bfc) and
# ``app.config.DEFAULT_DATABASE_SCHEMA``.
SCHEMA = "incident_agent"


def upgrade() -> None:
    """Create the investigation_reports table in the agent schema."""
    op.create_table(
        "investigation_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("probable_root_cause", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("root_cause_evidence_ids", sa.JSON(), nullable=False),
        sa.Column("suggested_fixes", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.investigation_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            [f"{SCHEMA}.incidents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_investigation_reports_job_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_investigation_reports_incident_id",
        "investigation_reports",
        ["incident_id"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the investigation_reports table (schema left in place)."""
    op.drop_index(
        "ix_investigation_reports_incident_id",
        table_name="investigation_reports",
        schema=SCHEMA,
    )
    op.drop_table("investigation_reports", schema=SCHEMA)
