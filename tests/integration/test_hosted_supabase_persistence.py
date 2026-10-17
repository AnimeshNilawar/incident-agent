r"""Opt-in persistence test against the dedicated Incident Agent database.

This is the only test that writes to a hosted database, so it is *opt-in* and
skipped unless an operator enables it explicitly. Ordinary ``pytest`` runs (and
CI) stay offline: nothing here reads the developer's ``.env``, and the target
is read from its own environment variable rather than ``DATABASE_URL``.

Enabling it (see ``docs/database-and-migrations.md``)::

    $env:INCIDENT_AGENT_HOSTED_TEST_DATABASE_URL = "postgresql+psycopg://..."
    $env:INCIDENT_AGENT_HOSTED_TEST_ALLOW = "I_UNDERSTAND_THIS_WRITES_TO_THE_AGENT_DATABASE"
    python -m pytest -q tests/integration/test_hosted_supabase_persistence.py -m hosted_integration

Target guard — the run is refused unless **all** of the following hold
(``_verify_target``), every check read-only:

1. ``INCIDENT_AGENT_HOSTED_TEST_DATABASE_URL`` is set and parses as a
   PostgreSQL URL naming a database.
2. ``INCIDENT_AGENT_HOSTED_TEST_ALLOW`` equals the exact confirmation string.
3. The ``incident_agent`` schema exists and contains both agent tables
   (``incidents``, ``investigation_jobs``).
4. Neither agent table exists in ``public`` (would mean a mis-migrated target).
5. ``incident_agent.alembic_version`` exists and its revision equals the
   repository's current Alembic head, so the target is migrated to exactly the
   schema these models expect.
6. The connection target is explicitly verified as the dedicated Incident Agent
   database before inserting test records (schema, required tables, no public
   leakage, and matching Alembic head). The test does not assume a particular
   least-privilege role beyond the ability to read/write the agent tables;
   any role approved by the operator may be used.

Data safety — the test tracks the exact primary keys of every row it creates
and deletes **only those** in fixture teardown, which runs even when an
assertion fails. It deletes the child ``investigation_jobs`` rows before their
parent incidents (explicitly), asserts the deleted row counts match the tracked
keys exactly, re-queries to confirm they are gone, and finally asserts that the
count of *unrelated* rows in both tables is unchanged. No ``TRUNCATE`` and no
predicate other than the tracked primary keys are ever used. The engine never
runs Alembic, ``create_all``, or any DDL — the schema is only inspected.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.session import get_db, postgres_connect_args
from app.main import create_app
from app.models.incident import Incident
from app.models.investigation_job import InvestigationJob
from app.schemas.incident import IncidentCreate
from app.services.incidents import create_or_update_incident
from app.services.investigation_jobs import create_investigation_job
from app.services.job_lifecycle import ClaimOutcome, claim_job, complete_job

pytestmark = pytest.mark.hosted_integration

TARGET_URL_ENV = "INCIDENT_AGENT_HOSTED_TEST_DATABASE_URL"
ALLOW_ENV = "INCIDENT_AGENT_HOSTED_TEST_ALLOW"
ALLOW_VALUE = "I_UNDERSTAND_THIS_WRITES_TO_THE_AGENT_DATABASE"

REPO_ROOT = Path(__file__).resolve().parents[2]

SCHEMA = settings.database_schema
AGENT_TABLES = {"incidents", "investigation_jobs"}

API_TEST_USERNAME = "hosted-smoke-api-user"
API_TEST_PASSWORD = "hosted-smoke-api-password"


def _expected_head_revision() -> str:
    """Return this repository's current Alembic head, without touching a DB."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def _verify_target(engine: Engine) -> None:
    """Refuse to run unless this is the dedicated, migrated agent database."""
    expected_head = _expected_head_revision()

    with engine.connect() as connection:
        inspector = inspect(connection)

        if SCHEMA not in set(inspector.get_schema_names()):
            pytest.fail(
                f"Schema '{SCHEMA}' does not exist in the target database. "
                "This test only runs against the migrated Incident Agent "
                "database and never creates a schema."
            )

        tables = set(inspector.get_table_names(schema=SCHEMA))
        if not AGENT_TABLES <= tables:
            pytest.fail(
                f"The '{SCHEMA}' schema is missing agent tables: "
                f"expected {sorted(AGENT_TABLES)}, found {sorted(tables)}. "
                "Apply the migration first; this test never runs Alembic."
            )

        leaked = AGENT_TABLES & set(inspector.get_table_names(schema="public"))
        if leaked:
            pytest.fail(
                "The agent tables exist in the 'public' schema "
                f"({sorted(leaked)}), which indicates a mis-migrated target."
            )

        if not inspector.has_table("alembic_version", schema=SCHEMA):
            pytest.fail(
                f"'{SCHEMA}.alembic_version' is missing, so the target is not "
                "a migrated Incident Agent database."
            )

        revision = connection.execute(
            text(f'SELECT version_num FROM "{SCHEMA}".alembic_version')
        ).scalar_one()

        if revision != expected_head:
            pytest.fail(
                f"The target is at revision {revision!r} but this repository's "
                f"Alembic head is {expected_head!r}. Refusing to run against a "
                "database whose schema these models were not checked against."
            )


@pytest.fixture(scope="session")
def hosted_engine() -> Iterator[Engine]:
    """Guarded engine bound to the explicitly configured hosted database."""
    raw = os.environ.get(TARGET_URL_ENV, "").strip()

    if not raw:
        pytest.skip(f"{TARGET_URL_ENV} is not set; hosted test skipped")

    allow = os.environ.get(ALLOW_ENV, "")

    if allow != ALLOW_VALUE:
        pytest.fail(
            f"{ALLOW_ENV} must be exactly '{ALLOW_VALUE}' to run a test that "
            "writes to the Incident Agent database."
        )

    parsed = make_url(raw)

    if parsed.get_backend_name() != "postgresql":
        pytest.fail(f"{TARGET_URL_ENV} must be a PostgreSQL URL.")

    if not parsed.database:
        pytest.fail(f"{TARGET_URL_ENV} must name a database.")

    engine = create_engine(
        raw,
        execution_options={"schema_translate_map": {None: SCHEMA}},
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args=postgres_connect_args(raw),
    )

    try:
        _verify_target(engine)
    except BaseException:
        engine.dispose()
        raise

    try:
        yield engine
    finally:
        engine.dispose()


class _Tracker:
    """Records the exact rows this test created, for exact cleanup."""

    def __init__(self) -> None:
        self.marker = f"hosted-smoke-{uuid.uuid4().hex}"
        self.incident_ids: list[uuid.UUID] = []
        self.job_ids: list[uuid.UUID] = []
        self.baseline_unrelated: tuple[int, int] = (0, 0)

    def track_incident(self, incident_id: uuid.UUID) -> None:
        self.incident_ids.append(incident_id)

    def track_job(self, job_id: uuid.UUID) -> None:
        self.job_ids.append(job_id)

    def _unrelated_counts(self, connection) -> tuple[int, int]:
        incidents = connection.execute(
            select(func.count())
            .select_from(Incident)
            .where(Incident.fingerprint.not_like(f"{self.marker}%"))
        ).scalar_one()

        jobs = connection.execute(
            select(func.count())
            .select_from(InvestigationJob)
            .where(InvestigationJob.idempotency_key.not_like(f"{self.marker}%"))
        ).scalar_one()

        return incidents, jobs

    def capture_baseline(self, engine: Engine) -> None:
        with engine.connect() as connection:
            self.baseline_unrelated = self._unrelated_counts(connection)

    def cleanup(self, engine: Engine) -> None:
        if not self.incident_ids and not self.job_ids:
            return

        with engine.begin() as connection:
            job_result = connection.execute(
                delete(InvestigationJob).where(
                    InvestigationJob.id.in_(self.job_ids)
                )
            )
            incident_result = connection.execute(
                delete(Incident).where(Incident.id.in_(self.incident_ids))
            )

        with engine.connect() as connection:
            assert job_result.rowcount == len(self.job_ids), (
                f"cleanup deleted {job_result.rowcount} job rows but the test "
                f"tracked {len(self.job_ids)}"
            )
            assert incident_result.rowcount == len(self.incident_ids), (
                f"cleanup deleted {incident_result.rowcount} incident rows but "
                f"the test tracked {len(self.incident_ids)}"
            )

            remaining_jobs = connection.execute(
                select(func.count())
                .select_from(InvestigationJob)
                .where(InvestigationJob.id.in_(self.job_ids))
            ).scalar_one()
            remaining_incidents = connection.execute(
                select(func.count())
                .select_from(Incident)
                .where(Incident.id.in_(self.incident_ids))
            ).scalar_one()

            assert remaining_jobs == 0 and remaining_incidents == 0, (
                "cleanup left rows behind: "
                f"{remaining_jobs} jobs, {remaining_incidents} incidents"
            )

            assert self._unrelated_counts(connection) == self.baseline_unrelated, (
                "cleanup changed the number of unrelated rows; it must only "
                "delete the records this test created"
            )


@pytest.fixture()
def hosted_db(hosted_engine) -> Iterator[tuple[sessionmaker, _Tracker]]:
    factory = sessionmaker(
        bind=hosted_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    tracker = _Tracker()
    tracker.capture_baseline(hosted_engine)

    try:
        yield factory, tracker
    finally:
        tracker.cleanup(hosted_engine)


def _application_with_hosted_db(factory: sessionmaker) -> FastAPI:
    application = create_app("api")

    def _hosted_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = _hosted_get_db
    return application


def test_incident_created_retrieved_and_deduplicated(hosted_db, monkeypatch):
    factory, tracker = hosted_db
    fingerprint = f"{tracker.marker}-incident"

    payload = {
        "fingerprint": fingerprint,
        "title": "Hosted persistence smoke incident",
        "source": "manual",
        "severity": "low",
        "service_name": "hosted-smoke",
        "summary": "Created by the opt-in hosted persistence test",
    }

    monkeypatch.setattr(settings, "incident_api_username", API_TEST_USERNAME)
    monkeypatch.setattr(settings, "incident_api_password", API_TEST_PASSWORD)

    application = _application_with_hosted_db(factory)
    auth = (API_TEST_USERNAME, API_TEST_PASSWORD)

    with TestClient(application) as client:
        created = client.post("/api/v1/incidents", json=payload, auth=auth)

    assert created.status_code == 200, created.text
    body = created.json()
    incident_id = uuid.UUID(body["id"])
    tracker.track_incident(incident_id)
    assert body["status"] == "received"

    with TestClient(application) as client:
        fetched = client.get(f"/api/v1/incidents/{incident_id}", auth=auth)
        listing = client.get(
            "/api/v1/incidents", params={"limit": 100}, auth=auth
        )

    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["fingerprint"] == fingerprint
    assert incident_id in {uuid.UUID(row["id"]) for row in listing.json()}

    with factory() as db:
        stored = db.get(Incident, incident_id)
        assert stored is not None
        assert stored.fingerprint == fingerprint
        assert stored.source == "manual"
        assert stored.severity == "low"
        assert stored.service_name == "hosted-smoke"
        assert stored.status == "received"

    with TestClient(application) as client:
        again = client.post("/api/v1/incidents", json=payload, auth=auth)

    assert again.status_code == 200, again.text
    assert uuid.UUID(again.json()["id"]) == incident_id

    with factory() as db:
        rows = db.scalars(
            select(Incident).where(Incident.fingerprint == fingerprint)
        ).all()
        assert len(rows) == 1
        assert rows[0].last_seen_at >= rows[0].first_seen_at
        job_count = db.scalar(
            select(func.count())
            .select_from(InvestigationJob)
            .where(InvestigationJob.incident_id == incident_id)
        )

    assert job_count == 0


def test_investigation_job_created_claimed_and_completed(hosted_db):
    factory, tracker = hosted_db

    payload = IncidentCreate(
        fingerprint=f"{tracker.marker}-job",
        title="Hosted persistence smoke investigation",
        source="manual",
        severity="medium",
        service_name="hosted-smoke",
        summary="Created by the opt-in hosted persistence test",
    )

    with factory() as db:
        incident, created = create_or_update_incident(db, payload)
        tracker.track_incident(incident.id)
        assert created is True

        job, job_created = create_investigation_job(db, incident)
        tracker.track_job(job.id)

        assert job_created is True
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.cloud_task_name is None

        job_id = job.id
        incident_id = incident.id
        db.commit()

    with factory() as db:
        stored = db.get(InvestigationJob, job_id)
        assert stored is not None
        assert stored.incident_id == incident_id
        assert stored.status == "pending"

    with factory() as db:
        incident = db.get(Incident, incident_id)
        _, created_again = create_investigation_job(db, incident)
        assert created_again is False
        assert (
            db.scalar(
                select(func.count())
                .select_from(InvestigationJob)
                .where(InvestigationJob.incident_id == incident_id)
            )
            == 1
        )

    with factory() as db:
        claim = claim_job(db, job_id=job_id, incident_id=incident_id)

    assert claim.outcome is ClaimOutcome.CLAIMED
    token = claim.claim_token
    assert token == 1

    with factory() as db:
        running = db.get(InvestigationJob, job_id)
        assert running.status == "running"
        assert running.attempts == 1
        assert running.started_at is not None

    with factory() as db:
        assert complete_job(db, job_id=job_id, claim_token=token) is True

    with factory() as db:
        completed = db.get(InvestigationJob, job_id)
        assert completed.status == "completed"
        assert completed.attempts == 1
        assert completed.completed_at is not None
        assert completed.last_error is None
        assert completed.cloud_task_name is None
