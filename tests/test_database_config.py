"""Unit tests for database configuration and the DB layer.

These run on SQLite and complete without any PostgreSQL server.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.config import (
    DEFAULT_DATABASE_SCHEMA,
    LOCAL_ENVIRONMENTS,
    Settings,
    format_settings_error,
)
from app.db.base import Base, resolve_database_schema
from app.db.session import build_engine, postgres_connect_args

REPO_ROOT = Path(__file__).resolve().parents[1]

POSTGRES_URL = "postgresql+psycopg://user:password@localhost:5432/incident_agent"


@pytest.fixture(autouse=True)
def _clear_database_env(monkeypatch):
    """Keep ambient DATABASE_URL/ENVIRONMENT from leaking into these tests."""
    for name in ("DATABASE_URL", "ENVIRONMENT", "DATABASE_SCHEMA"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_local_sqlite():
    settings = Settings()

    assert settings.environment in LOCAL_ENVIRONMENTS
    assert settings.is_local_environment is True
    assert settings.is_postgres is False
    assert settings.database_backend == "sqlite"
    assert settings.database_schema == DEFAULT_DATABASE_SCHEMA


def test_deployed_environment_requires_postgres_url():
    with pytest.raises(ValidationError):
        Settings(environment="production", database_url="sqlite:///./x.db")


def test_deployed_environment_rejects_empty_url():
    with pytest.raises(ValidationError):
        Settings(environment="production", database_url="")


def test_deployed_environment_accepts_postgres_url():
    settings = Settings(environment="production", database_url=POSTGRES_URL)

    assert settings.is_postgres is True
    assert settings.database_schema == DEFAULT_DATABASE_SCHEMA


def test_config_error_message_does_not_echo_the_database_password():
    # A deployed environment pointed at a non-PostgreSQL URL is rejected at
    # startup. Pydantic's raw ValidationError embeds the input mapping (the
    # password included), so the sanitized summary we surface must not.
    secret = "aB3secret"
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            environment="production",
            database_url=f"mysql://user:{secret}@db.host/x",
        )

    message = format_settings_error(excinfo.value)

    assert secret not in message
    assert "DATABASE_URL" in message


def test_importing_app_config_does_not_leak_the_database_password():
    # End-to-end guard for the real startup path: importing ``app.config``
    # builds the module-level Settings, and an import failure is what uvicorn
    # (and Cloud Logging) would print. The password must survive nowhere in it.
    secret = "aB3secret"
    env = os.environ.copy()
    env["ENVIRONMENT"] = "production"
    env["DATABASE_URL"] = f"mysql://user:{secret}@db.host/x"

    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert secret not in output
    assert "Invalid configuration" in output


@pytest.mark.parametrize(
    "value",
    ["1incident", "incident-agent", "incident agent", "incident;drop", ""],
)
def test_invalid_schema_identifier_is_rejected(value):
    with pytest.raises(ValidationError):
        Settings(database_schema=value)


def test_non_default_schema_is_rejected_as_fixed():
    # The schema is fixed because the migrations hard-code it; a different
    # value must fail loudly rather than silently diverge from the migrations.
    with pytest.raises(ValidationError) as excinfo:
        Settings(database_schema="custom_schema")

    assert "fixed" in str(excinfo.value).lower()


def test_schema_identifier_is_trimmed():
    assert Settings(database_schema="  incident_agent  ").database_schema == (
        DEFAULT_DATABASE_SCHEMA
    )


def test_resolve_database_schema_selects_schema_only_for_postgres():
    assert resolve_database_schema(POSTGRES_URL, "incident_agent") == (
        "incident_agent"
    )
    assert resolve_database_schema("sqlite:///./x.db", "incident_agent") is None


def test_sqlite_metadata_stays_unschemaed():
    # Regression guard: the shared models must stay unqualified for SQLite so
    # the existing test suite keeps working without schema translation.
    assert Base.metadata.schema is None
    assert Base.metadata.tables["incidents"].schema is None
    assert Base.metadata.tables["investigation_jobs"].schema is None


def test_sqlite_create_all_produces_expected_tables():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    try:
        Base.metadata.create_all(bind=engine)
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {"incidents", "investigation_jobs"} <= tables


def test_build_engine_uses_expected_dialects():
    sqlite_engine = build_engine("sqlite://")
    postgres_engine = build_engine(POSTGRES_URL)

    try:
        assert sqlite_engine.dialect.name == "sqlite"
        assert postgres_engine.dialect.name == "postgresql"
    finally:
        sqlite_engine.dispose()
        postgres_engine.dispose()


def test_psycopg_connect_args_disable_prepared_statements():
    assert postgres_connect_args(POSTGRES_URL) == {"prepare_threshold": None}
    assert (
        postgres_connect_args(
            "postgresql+psycopg2://user:password@localhost:5432/db"
        )
        == {}
    )
