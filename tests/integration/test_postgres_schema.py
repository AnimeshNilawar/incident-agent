"""PostgreSQL integration tests for schema isolation and migrations.

These verify that the Alembic migrations create the dedicated
``incident_agent`` schema, that the agent tables live there (and never in
``public``), that the migrated structure matches the models, that ordinary
incident/job operations work against the migrated tables, and that
re-running or downgrading the migrations is safe.

They are skipped unless ``POSTGRES_TEST_DATABASE_URL`` is set and never run
against a production database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.base import Base
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.schemas.incident import IncidentCreate
from app.services.incidents import create_or_update_incident
from app.services.investigation_jobs import create_investigation_job
from app.services.job_lifecycle import (
    ClaimOutcome,
    claim_job,
    complete_job,
)

pytestmark = pytest.mark.postgres_integration

REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA = settings.database_schema
AGENT_TABLES = {"incidents", "investigation_jobs"}


@contextmanager
def _connection(database_url):
    engine = create_engine(database_url, poolclass=NullPool)

    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def test_alembic_creates_dedicated_schema(migrated_postgres_url):
    with _connection(migrated_postgres_url) as connection:
        schemas = inspect(connection).get_schema_names()

    assert SCHEMA in schemas


def test_agent_tables_exist_in_dedicated_schema(migrated_postgres_url):
    with _connection(migrated_postgres_url) as connection:
        tables = set(inspect(connection).get_table_names(schema=SCHEMA))

    assert AGENT_TABLES <= tables


def test_agent_tables_are_absent_from_public_schema(migrated_postgres_url):
    with _connection(migrated_postgres_url) as connection:
        public_tables = set(inspect(connection).get_table_names(schema="public"))

    assert AGENT_TABLES.isdisjoint(public_tables)


@pytest.mark.parametrize("table_name", sorted(AGENT_TABLES))
def test_migrated_columns_match_models(migrated_postgres_url, table_name):
    with _connection(migrated_postgres_url) as connection:
        inspector = inspect(connection)
        reflected = {
            column["name"]: column["nullable"]
            for column in inspector.get_columns(table_name, schema=SCHEMA)
        }
        primary_key = inspector.get_pk_constraint(table_name, schema=SCHEMA)

    expected = {
        column.name: column.nullable
        for column in Base.metadata.tables[table_name].columns
    }

    assert reflected == expected
    assert primary_key["constrained_columns"] == ["id"]


def test_incident_unique_fingerprint_constraint(migrated_postgres_url):
    with _connection(migrated_postgres_url) as connection:
        uniques = inspect(connection).get_unique_constraints(
            "incidents", schema=SCHEMA
        )

    assert any(
        constraint["name"] == "uq_incidents_fingerprint"
        and constraint["column_names"] == ["fingerprint"]
        for constraint in uniques
    )


def test_investigation_job_foreign_key_unique_and_indexes(
    migrated_postgres_url,
):
    with _connection(migrated_postgres_url) as connection:
        inspector = inspect(connection)
        foreign_keys = inspector.get_foreign_keys(
            "investigation_jobs", schema=SCHEMA
        )
        uniques = inspector.get_unique_constraints(
            "investigation_jobs", schema=SCHEMA
        )
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes(
                "investigation_jobs", schema=SCHEMA
            )
        }

    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert foreign_key["constrained_columns"] == ["incident_id"]
    assert foreign_key["referred_schema"] == SCHEMA
    assert foreign_key["referred_table"] == "incidents"
    assert foreign_key["referred_columns"] == ["id"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"

    assert {tuple(unique["column_names"]) for unique in uniques} == {
        ("idempotency_key",)
    }

    assert indexes["ix_investigation_jobs_incident_id"]["column_names"] == [
        "incident_id"
    ]
    assert indexes["ix_investigation_jobs_status_created"]["column_names"] == [
        "status",
        "created_at",
    ]


def test_incident_creation_and_deduplication_on_postgres(pg_session_factory):
    payload = IncidentCreate(
        fingerprint=f"pg-schema-{uuid.uuid4()}",
        title="BackendHighCPU",
        source="grafana",
        severity="high",
        service_name="micuppa-backend-india",
        summary="CPU above threshold",
    )

    with pg_session_factory() as db:
        incident, created = create_or_update_incident(db, payload)
        assert created is True
        incident_id = incident.id

        # Reprocessing the same alert updates in place instead of duplicating.
        _, created_again = create_or_update_incident(db, payload)
        assert created_again is False

    with pg_session_factory() as db:
        rows = db.scalars(
            select(Incident).where(Incident.fingerprint == payload.fingerprint)
        ).all()

    assert len(rows) == 1
    assert rows[0].id == incident_id
    assert rows[0].service_name == "micuppa-backend-india"


def test_fingerprint_unique_constraint_is_enforced_on_postgres(
    pg_session_factory,
):
    fingerprint = f"pg-schema-dup-{uuid.uuid4()}"

    with pg_session_factory() as db:
        db.add(
            Incident(
                fingerprint=fingerprint,
                title="BackendHighCPU",
                source="grafana",
                severity="high",
                status="received",
                service_name="micuppa-backend-india",
            )
        )
        db.commit()

    with pg_session_factory() as db:
        db.add(
            Incident(
                fingerprint=fingerprint,
                title="BackendHighCPU (duplicate)",
                source="grafana",
                severity="high",
                status="received",
                service_name="micuppa-backend-india",
            )
        )

        with pytest.raises(IntegrityError):
            db.commit()

        db.rollback()


def test_investigation_job_creation_and_lifecycle_on_postgres(
    pg_session_factory,
):
    with pg_session_factory() as db:
        incident = Incident(
            fingerprint=f"pg-job-{uuid.uuid4()}",
            title="BackendHighCPU",
            source="grafana",
            severity="high",
            status="received",
            service_name="micuppa-backend-india",
        )
        db.add(incident)
        db.flush()

        job, created = create_investigation_job(db, incident)
        assert created is True

        job_id = job.id
        incident_id = incident.id
        db.commit()

        # A repeated event for the same incident does not create a new job.
        _, created_again = create_investigation_job(db, incident)
        assert created_again is False

    with pg_session_factory() as db:
        claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert claim.outcome == ClaimOutcome.CLAIMED
    token = claim.claim_token

    with pg_session_factory() as db:
        assert complete_job(db, job_id=job_id, claim_token=token) is True

    with pg_session_factory() as db:
        job = db.get(InvestigationJob, job_id)
        assert job.status == "completed"
        assert job.attempts == 1


def test_reapplying_migrations_is_safe(
    migrated_postgres_url,
    migrate_database,
    alembic_config_factory,
):
    # A second ``upgrade head`` on an already-migrated database is a no-op.
    migrate_database(migrated_postgres_url)

    script = ScriptDirectory.from_config(
        alembic_config_factory(migrated_postgres_url)
    )
    head = script.get_current_head()

    with _connection(migrated_postgres_url) as connection:
        inspector = inspect(connection)
        assert SCHEMA in inspector.get_schema_names()
        assert AGENT_TABLES <= set(inspector.get_table_names(schema=SCHEMA))

        version = connection.execute(
            text(f'SELECT version_num FROM "{SCHEMA}".alembic_version')
        ).scalar_one()

    assert version == head


def test_migration_downgrade_and_upgrade_round_trip(
    postgres_database_url,
    migrate_database,
    alembic_config_factory,
):
    migrate_database(postgres_database_url)

    command.downgrade(alembic_config_factory(postgres_database_url), "base")

    with _connection(postgres_database_url) as connection:
        tables_after_downgrade = set(
            inspect(connection).get_table_names(schema=SCHEMA)
        )

    assert AGENT_TABLES.isdisjoint(tables_after_downgrade)

    migrate_database(postgres_database_url)

    with _connection(postgres_database_url) as connection:
        tables_after_upgrade = set(
            inspect(connection).get_table_names(schema=SCHEMA)
        )

    assert AGENT_TABLES <= tables_after_upgrade


def test_alembic_check_ignores_tables_owned_by_the_host_application(
    migrated_postgres_url,
):
    """``alembic check`` must never report unrelated schemas as drift.

    A table owned by the host application is created in ``public`` and
    ``alembic check`` is executed exactly the way CI runs it. If the
    autogenerate filters were not scoped to the agent schema, ``public``
    tables would be proposed for deletion (and the agent's own tables could be
    reported as drift too), so the command would exit non-zero. Either way the
    host table must be left untouched.

    The check runs in a subprocess with a PostgreSQL ``DATABASE_URL`` so the
    metadata is schema-qualified exactly as it is for a real migration run.
    """
    with _connection(migrated_postgres_url) as connection:
        connection.execute(
            text("CREATE TABLE public.host_app_table (id integer PRIMARY KEY)")
        )
        connection.commit()

    env = os.environ.copy()
    env["DATABASE_URL"] = migrated_postgres_url.render_as_string(
        hide_password=False
    )
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "alembic check reported drift\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    with _connection(migrated_postgres_url) as connection:
        inspector = inspect(connection)
        assert inspector.has_table("host_app_table", schema="public")
        assert AGENT_TABLES <= set(inspector.get_table_names(schema=SCHEMA))
