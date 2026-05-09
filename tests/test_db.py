"""Tests for the inline schema migration helper."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from likesurgeon.db import _migrate_in_place, init_db, make_engine


def test_migrate_in_place_no_op_on_fresh_install(tmp_path: Path) -> None:
    """Fresh install: no `snapshot_items` table yet → migration is a no-op."""
    engine = make_engine(tmp_path / "fresh.sqlite")
    _migrate_in_place(engine)  # must not raise

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "snapshot_items" not in tables


def test_migrate_in_place_adds_columns_to_pre_0_3_db(tmp_path: Path) -> None:
    """Simulate a 0.2-shape DB (no is_available / unavailable_reason) and
    verify the migration adds both columns idempotently."""
    db_path = tmp_path / "legacy.sqlite"
    legacy = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy.begin() as conn:
        conn.execute(text("CREATE TABLE snapshot_items (id INTEGER PRIMARY KEY, title TEXT)"))

    engine = make_engine(db_path)
    _migrate_in_place(engine)

    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(snapshot_items)"))}
    assert "is_available" in cols
    assert "unavailable_reason" in cols


def test_migrate_in_place_is_idempotent(tmp_path: Path) -> None:
    """Calling the migration twice must not raise (`ALTER ... ADD COLUMN`
    is not idempotent on its own — the helper must guard with PRAGMA)."""
    db_path = tmp_path / "idem.sqlite"
    engine = make_engine(db_path)
    init_db(engine)  # creates snapshot_items with columns already present
    _migrate_in_place(engine)  # first migration call — no-op for the new columns
    _migrate_in_place(engine)  # second call — must also be a no-op, not error


def test_make_engine_runs_migration(tmp_path: Path) -> None:
    """`make_engine` itself wires `_migrate_in_place` so every command path
    transparently picks up the schema. Simulate a 0.2 DB and verify the
    columns appear after `make_engine` alone (no separate migration call)."""
    db_path = tmp_path / "auto.sqlite"
    legacy = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy.begin() as conn:
        conn.execute(text("CREATE TABLE snapshot_items (id INTEGER PRIMARY KEY, title TEXT)"))

    make_engine(db_path)  # should run the migration as a side effect

    verify = create_engine(f"sqlite:///{db_path}", future=True)
    with verify.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(snapshot_items)"))}
    assert "is_available" in cols
    assert "unavailable_reason" in cols
