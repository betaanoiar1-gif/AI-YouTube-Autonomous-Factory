"""Database engine and session management.

SQLite is the Phase 0 database (zero infrastructure, runs locally, easy to
back up as a single file). The schema is managed by Alembic migrations
(``alembic upgrade head``); ``init_db`` (``Base.metadata.create_all``) is a
convenience for tests and first-run development only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from factory.storage.models import Base


def _sqlite_file_path(database_url: str) -> Path | None:
    """Extract the database file path from a SQLite URL, or None for memory DBs.

    Follows the SQLAlchemy convention: ``sqlite:///name.db`` is relative,
    ``sqlite:////abs/name.db`` is absolute.
    """
    if "://" not in database_url:
        return None
    rest = database_url.split("://", 1)[1]
    if rest.startswith("//"):
        candidate = rest[1:]  # absolute path
    elif rest.startswith("/"):
        candidate = rest[1:]  # relative path
    else:
        return None  # ":memory:" or empty
    if not candidate or candidate == ":memory:":
        return None
    return Path(candidate.split("?", 1)[0])


def create_engine_from_url(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for the configured database URL."""
    if database_url.startswith("sqlite"):
        # Ensure the parent directory exists for file-based SQLite databases.
        db_path = _sqlite_file_path(database_url)
        if db_path is not None:
            db_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> Callable[[], Session]:
    """Return a factory producing new ORM sessions bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: Callable[[], Session]) -> Iterator[Session]:
    """Provide a session with commit-on-success / rollback-on-error semantics."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create all tables directly from ORM metadata.

    Used by tests and local development. Production schema changes go through
    Alembic migrations (``alembic upgrade head``) — see docs/architecture.md.
    """
    Base.metadata.create_all(engine)


def schema_is_initialized(session: Session) -> bool:
    """True when the Alembic-managed schema is present in the database."""
    from sqlalchemy import text

    try:
        session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return True
    except Exception:  # noqa: BLE001 - missing table simply means not initialized
        return False
