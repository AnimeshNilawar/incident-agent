"""Tests for the read-only database connectivity check.

``python -m app.db.check_connection`` must confirm reachability without ever
contacting a hosted database from the test suite, so these tests use a mocked
engine or a throwaway SQLite file. They also pin the two safety properties an
operator relies on before a migration: nothing is created or modified, and no
credential can reach stdout/stderr.
"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db import check_connection as check_connection_module
from app.db import session as db_session
from app.db.check_connection import check_connection, main

SECRET = "s3cr3t-local-only"
PG_URL = f"postgresql+psycopg://agent:{SECRET}@pooler.example.com:6543/postgres"


class FakeResult:
    def scalar(self):
        return 1


class FakeConnection:
    """Minimal connection double: records executed statements, never fails."""

    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement):
        self.statements.append(str(statement))
        return FakeResult()


class FakeEngine:
    def __init__(self):
        self.connection = FakeConnection()
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def sqlite_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_successful_check_prints_target_and_result(monkeypatch, capsys):
    engine = FakeEngine()
    monkeypatch.setattr(
        check_connection_module,
        "resolve_database_url",
        lambda *args, **kwargs: PG_URL,
    )
    monkeypatch.setattr(db_session, "build_engine", lambda url: engine)

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "connection=ok" in captured.out
    assert "host=pooler.example.com" in captured.out
    assert "password_set=True" in captured.out
    # Reuses the readiness probe: exactly one trivial statement, then disposal.
    assert engine.connection.statements == ["SELECT 1"]
    assert engine.disposed is True
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_failed_check_hides_exception_and_credentials(monkeypatch, capsys):
    class FailingEngine:
        def connect(self):
            raise OperationalError(
                "SELECT 1", {}, Exception(f"password authentication for {SECRET} failed")
            )

        def dispose(self):
            pass

    monkeypatch.setattr(
        check_connection_module,
        "resolve_database_url",
        lambda *args, **kwargs: PG_URL,
    )
    monkeypatch.setattr(db_session, "build_engine", lambda url: FailingEngine())

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "connection=failed" in captured.out
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "Traceback" not in captured.err


def test_unparseable_url_is_reported_without_echoing_it(monkeypatch, capsys):
    monkeypatch.setattr(
        check_connection_module,
        "resolve_database_url",
        lambda *args, **kwargs: f"not a url {SECRET}",
    )

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not a parseable SQLAlchemy URL" in captured.err
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_missing_target_exits_without_printing_anything(monkeypatch, capsys):
    monkeypatch.setattr(
        check_connection_module, "resolve_database_url", lambda *a, **k: ""
    )

    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "DATABASE_URL is not set." in captured.err


def test_engine_construction_failure_is_reported_not_raised(monkeypatch):
    def boom(url):
        raise ValueError(f"unusable driver for {SECRET}")

    monkeypatch.setattr(db_session, "build_engine", boom)

    assert check_connection(PG_URL) is False


def test_unloadable_session_helpers_are_reported_not_raised(monkeypatch):
    # ``app.db.session`` builds its own engine at import time, which can fail
    # for a malformed URL. The tool must report a failure, never a traceback.
    def boom():
        raise RuntimeError(f"cannot import engine for {SECRET}")

    monkeypatch.setattr(check_connection_module, "session_helpers", boom)

    assert check_connection(PG_URL) is False


def test_resolver_reads_the_env_file_through_settings(tmp_path, monkeypatch):
    # The check tool must resolve its target the same way the app does:
    # through ``Settings``, which honours ``.env``. Use an isolated temp file
    # so the developer's real ``.env`` is never read.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    expected = sqlite_url(tmp_path / "from_env.db")
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={expected}\n", encoding="utf-8")

    resolved = check_connection_module.resolve_database_url(
        Settings(_env_file=env_file)
    )

    assert resolved == expected


def test_check_connection_runs_select_1_on_sqlite(tmp_path):
    url = sqlite_url(tmp_path / "probe.db")

    assert check_connection(url) is True

    # Read-only: connecting must not have created a single table.
    probe = create_engine(url)
    try:
        assert inspect(probe).get_table_names() == []
    finally:
        probe.dispose()


def test_check_creates_no_tables_and_modifies_no_data(tmp_path, monkeypatch):
    path = tmp_path / "existing.db"
    url = sqlite_url(path)

    seed = create_engine(url)
    with seed.begin() as connection:
        connection.execute(
            text("CREATE TABLE marker (id INTEGER PRIMARY KEY, note TEXT)")
        )
        connection.execute(text("INSERT INTO marker (note) VALUES ('unchanged')"))
    seed.dispose()

    monkeypatch.setattr(
        check_connection_module, "resolve_database_url", lambda *a, **k: url
    )

    exit_code = main()
    assert exit_code == 0

    verify = create_engine(url)
    try:
        # No incidents/investigation_jobs/alembic_version appeared, and the
        # existing row is untouched: no DDL, no migration, no write.
        assert set(inspect(verify).get_table_names()) == {"marker"}
        with verify.connect() as connection:
            note = connection.execute(text("SELECT note FROM marker")).scalar_one()
    finally:
        verify.dispose()

    assert note == "unchanged"
