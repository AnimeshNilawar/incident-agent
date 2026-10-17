"""Alembic migration environment for the Micuppa Incident Agent.

The database URL is resolved at runtime (never committed) in this order:

1. ``-x db_url=...`` on the Alembic command line;
2. a ``db_url`` attribute set on the Alembic ``Config`` object (used by the
   integration tests);
3. the ``sqlalchemy.url`` main option;
4. the application's ``DATABASE_URL`` setting.

For PostgreSQL the migrations intentionally target a dedicated schema
(``DATABASE_SCHEMA``, default ``incident_agent``) so the agent's tables never
land in the host application's ``public`` schema. The schema is created if it
does not exist before Alembic writes its own ``alembic_version`` table into
it.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL, make_url
from sqlalchemy.schema import CreateSchema

import app.models  # noqa: F401  # Registers every table on Base.metadata
from app.config import settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Set while migrations run so the autogenerate filters know the target
# dialect without relying on the Alembic context proxy.
_ACTIVE_DIALECT: dict[str, str | None] = {"name": None}


def _on_postgres() -> bool:
    return _ACTIVE_DIALECT["name"] == "postgresql"


def _database_url() -> str | URL:
    xargs = context.get_x_argument(as_dictionary=True)
    if xargs.get("db_url"):
        return xargs["db_url"]

    override = (config.attributes or {}).get("db_url")
    if override:
        return override

    return config.get_main_option("sqlalchemy.url") or settings.database_url


def _schema_name() -> str:
    return settings.database_schema


def _is_postgres(url: str | URL) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def include_object(object_, name, type_, reflected, compare_to):
    """Keep autogenerate scoped to the agent's own schema.

    The agent shares its database with the host application, so tables in
    other schemas (typically ``public``) must never be treated as drift and
    dropped. On PostgreSQL the agent's tables always carry the dedicated
    schema, while the default schema reflects as ``schema=None`` and belongs
    to the host application.
    """
    if type_ != "table":
        return True

    schema = getattr(object_, "schema", None)

    if _on_postgres():
        return schema == _schema_name()

    return schema in (None, _schema_name())


def include_name(name, type_, parent_names):
    """Restrict reflected schemas to the agent's schema."""
    if type_ != "schema":
        return True

    if _on_postgres():
        return name == _schema_name()

    return name in (None, _schema_name())


def run_migrations_offline() -> None:
    """Run migrations without a DBAPI, emitting SQL to stdout."""
    url = _database_url()
    is_postgres = _is_postgres(url)
    _ACTIVE_DIALECT["name"] = make_url(url).get_backend_name()

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=is_postgres,
        include_object=include_object,
        include_name=include_name,
        version_table_schema=_schema_name() if is_postgres else None,
        compare_type=True,
    )

    with context.begin_transaction():
        # The alembic_version table is created before any migration runs, so
        # the schema must exist first when generating offline SQL.
        if is_postgres:
            context.execute(f'CREATE SCHEMA IF NOT EXISTS "{_schema_name()}"')

        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    url = _database_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        is_postgres = connection.dialect.name == "postgresql"
        _ACTIVE_DIALECT["name"] = connection.dialect.name

        if is_postgres:
            # Create the target schema before Alembic provisions its own
            # version table inside it. ``IF NOT EXISTS`` makes this safe to
            # repeat on every run.
            connection.execute(CreateSchema(_schema_name(), if_not_exists=True))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=is_postgres,
            include_object=include_object,
            include_name=include_name,
            version_table_schema=_schema_name() if is_postgres else None,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
