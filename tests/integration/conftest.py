"""Shared fixtures for PostgreSQL integration tests.

These tests only run when ``POSTGRES_TEST_DATABASE_URL`` is explicitly set.
For every test a throwaway database with a unique name is created, the schema
is built inside it with the real Alembic migrations, and the database is
dropped afterwards. An existing shared database is never truncated, modified,
or reused, and the suite refuses to run if the configured URL appears to
target production.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

ENV_TEST_URL = "POSTGRES_TEST_DATABASE_URL"

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Refuse to run against anything that looks like a hosted/production DB.
PRODUCTION_MARKERS = (
    "supabase",
    "amazonaws",
    "rds.",
    "neon.tech",
    "azure",
    "cloudsql",
    "googleusercontent",
    "heroku",
)


def _require_test_url():
    """Return the configured test URL, skipping or failing safely.

    A missing variable skips the integration tests so the ordinary SQLite
    suite keeps running. A URL that looks like production fails loudly
    instead of silently touching live data.
    """
    raw = os.environ.get(ENV_TEST_URL, "").strip()

    if not raw:
        pytest.skip(
            f"{ENV_TEST_URL} is not set; PostgreSQL integration tests are "
            "skipped"
        )

    lowered = raw.lower()
    if any(marker in lowered for marker in PRODUCTION_MARKERS):
        pytest.fail(
            f"{ENV_TEST_URL} appears to target production; refusing to run "
            "integration tests against it"
        )

    url = make_url(raw)
    if not url.database:
        pytest.fail(
            f"{ENV_TEST_URL} must include a database to connect to the "
            "PostgreSQL server"
        )

    return url


@pytest.fixture(scope="session")
def _pg_test_url():
    """Session-scoped, validated connection URL for the isolated test DB."""
    return _require_test_url()


def _alembic_config(database_url: URL | str) -> Config:
    """Build an Alembic config that migrates the given database.

    The URL is passed through ``Config.attributes`` (not the ini file) so a
    password is never written to disk or interpolated by configparser.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["db_url"] = database_url
    return config


def run_migrations(database_url: URL | str) -> None:
    """Apply every migration (``upgrade head``) to ``database_url``."""
    command.upgrade(_alembic_config(database_url), "head")


@pytest.fixture()
def postgres_database_url(_pg_test_url):
    """Yield a fresh, throwaway PostgreSQL database URL (no migrations)."""
    admin_engine = create_engine(
        _pg_test_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )

    test_db = f"incident_agent_test_{uuid.uuid4().hex[:12]}"

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{test_db}"'))

    try:
        yield _pg_test_url.set(database=test_db)
    finally:
        with admin_engine.connect() as conn:
            # Terminate any leftover sessions before dropping the database.
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db"
                ),
                {"db": test_db},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}"'))

        admin_engine.dispose()


@pytest.fixture()
def migrated_postgres_url(postgres_database_url):
    """Yield a throwaway database URL that has been migrated to head."""
    run_migrations(postgres_database_url)
    return postgres_database_url


@pytest.fixture()
def migrate_database():
    """Return a callable that applies migrations to a database URL.

    Exposed as a fixture so integration modules can re-run migrations (or
    migrate a database they created themselves) without importing private
    conftest helpers.
    """
    return run_migrations


@pytest.fixture()
def alembic_config_factory():
    """Return a factory building an Alembic ``Config`` for a database URL."""
    return _alembic_config


@pytest.fixture()
def postgres_engine(migrated_postgres_url):
    """Yield an engine bound to a freshly migrated PostgreSQL database.

    The models carry no explicit schema while the test suite runs with the
    SQLite default settings, so a ``schema_translate_map`` points unqualified
    table references at the dedicated agent schema. ORM operations therefore
    exercise the tables created by the migrations.
    """
    engine = create_engine(
        migrated_postgres_url,
        execution_options={
            "schema_translate_map": {None: settings.database_schema},
        },
    )

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def pg_session_factory(postgres_engine):
    """Yield a factory that opens independent sessions per thread."""
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        expire_on_commit=False,
    )
