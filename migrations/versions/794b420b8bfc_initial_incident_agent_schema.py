"""initial incident agent schema

Creates the dedicated ``incident_agent`` schema and the two agent tables in
it. Nothing is created in the PostgreSQL ``public`` schema.

Revision ID: 794b420b8bfc
Revises:
Create Date: 2026-10-17 20:18:50.401223

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "794b420b8bfc"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema name is fixed and duplicated from
# ``app.config.DEFAULT_DATABASE_SCHEMA`` on purpose: a migration file must stay
# reproducible and independent of runtime settings, so it cannot follow a
# configurable ``DATABASE_SCHEMA``. ``Settings`` rejects any other value and
# ``alembic revision --autogenerate`` always emits the schema as a literal, so
# targeting a different schema means regenerating the migrations.
SCHEMA = "incident_agent"


def upgrade() -> None:
    """Create the ``incident_agent`` schema and its tables."""
    # The migration environment also creates the schema before provisioning
    # the alembic_version table, but creating it here keeps the migration
    # self-contained and safe to re-run.
    op.execute(sa.schema.CreateSchema(SCHEMA, if_not_exists=True))

    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("service_name", sa.String(length=150), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_incidents_fingerprint"),
        schema=SCHEMA,
    )
    op.create_table(
        "investigation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("cloud_task_name", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            [f"{SCHEMA}.incidents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_investigation_jobs_incident_id",
        "investigation_jobs",
        ["incident_id"],
        unique=False,
        schema=SCHEMA,
    )
    op.create_index(
        "ix_investigation_jobs_status_created",
        "investigation_jobs",
        ["status", "created_at"],
        unique=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the agent tables.

    The ``incident_agent`` schema itself is intentionally left in place so
    Alembic's own ``alembic_version`` table (which lives in that schema) is
    not removed while the migration is still running.
    """
    op.drop_index(
        "ix_investigation_jobs_status_created",
        table_name="investigation_jobs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_investigation_jobs_incident_id",
        table_name="investigation_jobs",
        schema=SCHEMA,
    )
    op.drop_table("investigation_jobs", schema=SCHEMA)
    op.drop_table("incidents", schema=SCHEMA)
