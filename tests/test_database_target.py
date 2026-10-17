"""Tests for the safe database-target inspector.

``describe_database_target`` backs the pre-connection check documented in
``docs/database-and-migrations.md``. It must expose enough to confirm the
target (host, port, database, role, TLS mode) while never revealing the
password, so these tests assert on both.
"""

from app.db.target import describe_database_target

SECRET = "s3cr3t-local-only"


def test_reports_fields_without_the_password():
    target = describe_database_target(
        f"postgresql+psycopg://agent:{SECRET}@pooler.example.com:6543/postgres"
    )

    assert target["dialect"] == "postgresql"
    assert target["driver"] == "psycopg"
    assert target["host"] == "pooler.example.com"
    assert target["port"] == 6543
    assert target["database"] == "postgres"
    assert target["username"] == "agent"
    assert target["password_set"] is True
    assert SECRET not in repr(target)


def test_reports_absent_password():
    target = describe_database_target("sqlite:///./incident_agent.db")

    assert target["dialect"] == "sqlite"
    assert target["password_set"] is False


def test_reports_tls_mode():
    target = describe_database_target(
        f"postgresql+psycopg://agent:{SECRET}@host:5432/postgres?sslmode=require"
    )

    assert target["sslmode"] == "require"
