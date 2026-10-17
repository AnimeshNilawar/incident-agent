import logging
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


def postgres_connect_args(database_url: str) -> dict[str, object]:
    """Return driver connect arguments for a PostgreSQL URL.

    Supabase is reached through a transaction-mode connection pooler, where
    server-side prepared statements cannot be routed safely across
    transactions. psycopg (v3) lets us disable automatic preparation with
    ``prepare_threshold=None``. The argument is gated on the driver name
    because psycopg2 would reject it.
    """
    if make_url(database_url).get_driver_name() == "psycopg":
        return {"prepare_threshold": None}

    return {}


def build_engine(database_url: str) -> Engine:
    """Create an engine configured for the target database.

    SQLite keeps the single-connection development defaults. PostgreSQL is
    assumed to sit behind a connection pooler, so idle connections are
    recycled ahead of the pooler's timeout and ``pool_pre_ping`` discards
    connections the pooler already closed.
    """
    if database_url.startswith("sqlite"):
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

    return create_engine(
        database_url,
        connect_args=postgres_connect_args(database_url),
        pool_pre_ping=True,
        pool_recycle=1800,
    )


engine = build_engine(settings.database_url)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def database_is_reachable(bind: Engine | None = None) -> bool:
    """Check that a connection can be opened and a trivial query executed.

    Backs ``/health/ready``. Only database connectivity is validated: Cloud
    Tasks and Google's token service are outbound dependencies that a
    readiness probe must not call on every request, so they are deliberately
    not probed here (see ``docs/deployment.md``).

    The database URL and the underlying driver error are never returned or
    logged by this helper, so a failing readiness probe cannot leak
    credentials. It returns ``False`` instead of raising, so an unhealthy
    database yields a ``503`` rather than a traceback.
    """
    target = bind if bind is not None else engine

    try:
        with target.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        # Deliberately broad: any failure to reach the database (connection
        # refused, wrong credentials, pooler closed the link) means "not
        # ready" and must become a 503, never a 500 traceback. Only the
        # exception type is logged, for triage: the URL, the message and the
        # traceback are omitted because they can carry credentials.
        logger.warning(
            "Readiness check could not reach the database (%s)",
            type(exc).__name__,
        )
        return False

    return True
