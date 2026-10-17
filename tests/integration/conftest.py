"""Shared fixtures for PostgreSQL integration tests.

These tests only run when ``POSTGRES_TEST_DATABASE_URL`` is explicitly set.
For every test a throwaway database with a unique name is created, the schema
is built inside it, and the database is dropped afterwards. An existing
shared database is never truncated, modified, or reused, and the suite
refuses to run if the configured URL appears to target production.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # Registers the tables on Base.metadata
from app.db.base import Base

ENV_TEST_URL = "POSTGRES_TEST_DATABASE_URL"

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


@pytest.fixture()
def postgres_engine(_pg_test_url):
    """Yield an engine bound to a fresh, throwaway PostgreSQL database.

    A brand new database is created for every test and dropped afterwards,
    so tests are fully isolated from each other and from any existing data.
    """
    admin_engine = create_engine(
        _pg_test_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )

    test_db = f"incident_agent_test_{uuid.uuid4().hex[:12]}"

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{test_db}"'))

    test_engine = create_engine(_pg_test_url.set(database=test_db))

    try:
        Base.metadata.create_all(bind=test_engine)
        yield test_engine
    finally:
        test_engine.dispose()

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
def pg_session_factory(postgres_engine):
    """Yield a factory that opens independent sessions per thread."""
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        expire_on_commit=False,
    )
