from sqlalchemy import MetaData
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


def resolve_database_schema(
    database_url: str,
    configured_schema: str,
) -> str | None:
    """Return the schema the shared metadata should use, if any.

    PostgreSQL targets a dedicated schema so the agent's tables, indexes, and
    foreign keys never land in the host application's ``public`` schema.
    SQLite has no equivalent isolation and the existing tests build
    unqualified tables, so no schema is applied there.
    """
    if make_url(database_url).get_backend_name() == "postgresql":
        return configured_schema

    return None


# Centralised so every model (and Alembic autogenerate) agrees on the schema
# without per-model edits. ``schema_translate_map`` remains the escape hatch
# if a table ever needs to be rendered into a different schema at runtime;
# the PostgreSQL integration tests rely on it to run the same models against
# a test database.
_SCHEMA = resolve_database_schema(
    settings.database_url,
    settings.database_schema,
)


class Base(DeclarativeBase):
    """Base class for all database models."""

    metadata = MetaData(schema=_SCHEMA)
