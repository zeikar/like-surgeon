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


def _migrate_in_place(engine: Engine) -> None:
    """Idempotently add columns introduced after a table was first created.

    SQLite supports ``ALTER TABLE ... ADD COLUMN`` for nullable columns
    without rewriting existing rows. This helper guards each ALTER with a
    ``PRAGMA table_info`` check so it's safe to call on every engine
    construction. Tables that don't exist yet are skipped — fresh installs
    let ``init_db`` create them with the columns already in
    ``Base.metadata``.
    """
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "snapshot_items" not in tables:
            return
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(snapshot_items)")}
        for name, decl in (
            ("is_available", "BOOLEAN"),
            ("unavailable_reason", "VARCHAR(32)"),
        ):
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE snapshot_items ADD COLUMN {name} {decl}")


def make_engine(db_path: Path) -> Engine:
    """Build a SQLite engine pointing at ``db_path``.

    Runs ``_migrate_in_place`` as a side effect so every CLI command path
    transparently bootstraps any 0.3+ schema additions on existing DBs.
    """
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)
    _migrate_in_place(engine)
    return engine


def init_db(engine: Engine) -> None:
    """Create all tables if missing.

    For 0.3+ schema additions to existing DBs, see ``_migrate_in_place``
    (called from ``make_engine``) — that path adds new nullable columns
    without rewriting existing rows. ``init_db`` itself only handles the
    "no tables yet" case.
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
