"""Verify that the configured database is reachable and authenticates.

``python -m app.db.check_connection`` opens one connection to the target
described by ``DATABASE_URL`` and runs a trivial ``SELECT 1``. It exists so an
operator can confirm *real* connectivity and credentials before the first
hosted migration, without importing Alembic or touching the schema.

The check is deliberately read-only:

* it never creates tables, runs migrations, or writes data — ``SELECT 1`` is
  the only statement executed;
* it prints only the non-secret target fields (reusing
  ``app.db.target.describe_database_target``) and the connection result;
* it never prints ``DATABASE_URL``, the password, a traceback, or any driver
  message, because those can embed the connection string. Failure is reported
  as ``connection=failed`` plus the exception *type* only, which is what
  ``app.db.session.database_is_reachable`` already guarantees.

Configuration comes from the application's own ``Settings`` (including ``.env``)
through ``app.db.target.resolve_database_url``, and the engine is built by
``app.db.session.build_engine``, so the tool exercises exactly the same
connection path the application will use — including the pooler-safe
``prepare_threshold=None`` connect argument.

The session helpers are imported lazily on purpose: ``app.db.session`` builds
its module-level engine at import time, and an unusable ``DATABASE_URL`` (for
example an unknown driver scheme) would otherwise crash the tool with a raw
traceback before ``main()`` could report anything.

Exit codes: ``0`` reachable, ``1`` failed, ``2`` no target configured.
"""

from __future__ import annotations

import sys

from app.db.target import describe_database_target, resolve_database_url


def session_helpers():
    """Return the engine builder and the reachability probe.

    Imported on each call so the tool stays importable even when
    ``app.db.session`` cannot build its own engine (see the module docstring),
    and so tests can substitute them at the source module.
    """
    from app.db.session import build_engine, database_is_reachable

    return build_engine, database_is_reachable


def check_connection(database_url: str) -> bool:
    """Open one connection and execute ``SELECT 1``.

    Returns ``True`` when the database answered. Any failure — unresolvable
    host, refused connection, rejected credentials, unusable driver — becomes
    ``False`` rather than an exception, and the URL, password and driver
    message are never surfaced. The engine created here is always disposed.

    Read-only: no table is created, no migration is executed and no row is
    written.
    """
    try:
        build_engine, database_is_reachable = session_helpers()
    except Exception:  # noqa: BLE001 - an unusable driver is a failure, not a crash
        return False

    try:
        engine = build_engine(database_url)
    except Exception:  # noqa: BLE001 - driver import/URL errors are failures
        # ``create_engine`` errors can quote the connection string, so they are
        # collapsed to a boolean without any of their text reaching output.
        return False

    try:
        return database_is_reachable(engine)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    raw = resolve_database_url().strip()

    if not raw:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    try:
        target = describe_database_target(raw)
    except Exception:  # noqa: BLE001 - a parse error can embed the raw URL
        # Never echo the raw value: SQLAlchemy's parse error can include the
        # whole connection string, password and all.
        print(
            "DATABASE_URL is not a parseable SQLAlchemy URL.",
            file=sys.stderr,
        )
        return 1

    # The target is printed first so a failed check still shows *which*
    # database was attempted.
    for key, value in target.items():
        print(f"{key}={value}")

    try:
        reachable = check_connection(raw)
    except Exception:  # noqa: BLE001 - anything unexpected still reports safely
        reachable = False

    if reachable:
        print("connection=ok")
        return 0

    print("connection=failed")
    print(
        "Could not reach the database. The URL, password and driver error "
        "are deliberately omitted; confirm host, port, username and sslmode "
        "in the target above.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
