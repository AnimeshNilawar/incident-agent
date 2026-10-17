"""Describe the configured database target without revealing its secret.

Operators point the agent at a PostgreSQL database by setting
``DATABASE_URL``. Before the first connection it is worth confirming *which*
host, port, database and role the URL actually targets — but pasting the whole
URL would expose the password, so ``python -m app.db.target`` prints only the
non-secret parts of the target plus whether a password is present.

The module parses the URL locally and never opens a network connection, so it
is safe to run before the database is reachable. See
``docs/database-and-migrations.md`` for the full pre-connection checklist.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy.engine import make_url


def describe_database_target(database_url: str) -> dict[str, object]:
    """Return the non-secret descriptors of a SQLAlchemy database URL.

    The password is reported only as a boolean so the result can be printed,
    pasted into a ticket, or logged freely.
    """
    url = make_url(database_url)

    return {
        "dialect": url.get_backend_name(),
        "driver": url.get_driver_name(),
        "host": url.host,
        "port": url.port,
        "database": url.database,
        "username": url.username,
        "password_set": url.password is not None,
        "sslmode": url.query.get("sslmode"),
    }


def main(argv: list[str] | None = None) -> int:
    raw = os.environ.get("DATABASE_URL", "").strip()

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

    for key, value in target.items():
        print(f"{key}={value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
