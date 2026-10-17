"""Tests for the safe database-target inspector.

``describe_database_target`` backs the pre-connection check documented in
``docs/database-and-migrations.md``. It must expose enough to confirm the
target (host, port, database, role, TLS mode) while never revealing the
password, so these tests assert on both.

The URL is resolved through ``app.config.Settings``, so these tests also cover
``.env`` loading and its precedence over the process environment. They supply
their own isolated ``.env`` file (via ``Settings(_env_file=...)``) so they never
read or modify the developer's real ``.env`` file.
"""

from app.config import Settings
from app.db import target
from app.db.target import describe_database_target, main, resolve_database_url

SECRET = "s3cr3t-local-only"

FILE_URL = (
    "postgresql+psycopg://fileuser:filepw@filehost.example.com:5432/fromfile"
)
ENV_URL = "postgresql+psycopg://envuser:envpw@envhost.example.com:5432/fromenv"


def write_env_file(tmp_path, database_url: str):
    """Create an isolated ``.env`` under pytest's temporary directory."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={database_url}\n", encoding="utf-8")
    return env_file


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


def test_resolves_database_url_from_the_env_file(tmp_path, monkeypatch):
    """With no process override, the value comes from the ``.env`` file."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = write_env_file(tmp_path, FILE_URL)

    resolved = resolve_database_url(Settings(_env_file=env_file))

    assert resolved == FILE_URL

    described = describe_database_target(resolved)
    assert described["host"] == "filehost.example.com"
    assert described["username"] == "fileuser"
    assert described["password_set"] is True
    assert "filepw" not in repr(described)


def test_process_environment_overrides_the_env_file(tmp_path, monkeypatch):
    """An explicitly set ``DATABASE_URL`` wins over the ``.env`` value."""
    env_file = write_env_file(tmp_path, FILE_URL)
    monkeypatch.setenv("DATABASE_URL", ENV_URL)

    resolved = resolve_database_url(Settings(_env_file=env_file))

    assert resolved == ENV_URL
    assert "envhost.example.com" in resolved
    assert "filehost.example.com" not in resolved


def test_main_prints_the_configured_target_without_the_password(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        target,
        "configured_settings",
        Settings(
            database_url=(
                f"postgresql+psycopg://agent:{SECRET}"
                "@pooler.example.com:6543/postgres"
            )
        ),
    )

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "host=pooler.example.com" in captured.out
    assert "password_set=True" in captured.out
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_main_reports_an_unconfigured_target(monkeypatch, capsys):
    monkeypatch.setattr(
        target, "configured_settings", Settings(database_url="")
    )

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "DATABASE_URL is not set." in captured.err


def test_main_never_echoes_an_unparseable_url(monkeypatch, capsys):
    # A SQLAlchemy parse error embeds the raw connection string, so it must
    # never reach stderr alongside the configured password.
    monkeypatch.setattr(
        target,
        "configured_settings",
        Settings(database_url=f"not a url {SECRET}"),
    )

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not a parseable SQLAlchemy URL" in captured.err
    assert SECRET not in captured.err
