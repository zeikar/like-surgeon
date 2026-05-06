"""SQLAlchemy engine, session factory, and schema initializer."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


@event.listens_for(Engine, "connect")
def _sqlite_fk_pragma(dbapi_connection, _connection_record) -> None:
    """Enable foreign-key enforcement for every SQLite connection.

    SQLite ships with FK enforcement off by default; without this hook our
    ``ondelete=CASCADE`` / ``ondelete=RESTRICT`` declarations would be
    silently advisory.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(db_path: Path) -> Engine:
    """Build a SQLite engine pointing at ``db_path``."""
    url = f"sqlite:///{db_path}"
    return create_engine(url, future=True)


def init_db(engine: Engine) -> None:
    """Create all tables if missing.

    0.2 ships with a breaking schema change vs. 0.1: the YouTube Music source
    string was renamed, three classifier columns were added to SnapshotItem,
    Snapshot/Track ``created_at``/``updated_at`` became timezone-aware, and
    two new tables were introduced for cross-source compare output —
    ``Diagnosis`` (one row per ``compare-likes`` run) and ``DiagnosisItem``
    (one row per persisted issue, FK to Diagnosis with cascade). Existing
    0.1 DBs must be deleted and rescanned — a deliberate MVP choice; see
    README's "Upgrading from 0.1" section. Alembic / proper migrations land
    in 0.3+ once there's a real user base to protect.
    """
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional session context. Commits on success, rolls back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
