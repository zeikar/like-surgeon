# like-surgeon MVP 0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Python CLI MVP (`likesurgeon`) that authenticates with YouTube Music, fetches liked songs, persists local SQLite snapshots, diffs/exports them, and prints a basic health report — all read-only except for local DB writes.

**Architecture:** Layered design — a Typer CLI calls service functions (`snapshot`, `diff`, `export`, `doctor`) that depend on a SQLAlchemy ORM (`models.py` + `db.py`) backed by SQLite. External access (`ytmusicapi`) is wrapped behind a thin `YTMusicClient` so it can be swapped or mocked. Pydantic DTOs sit between the ORM and CLI/JSON output. `normalize.py` is pure-functional (canonical key generation) so it's easy to unit test and re-use later for the matching engine in MVP 0.3.

**Tech Stack:** Python ≥3.11 · uv · Typer · Rich · Pydantic v2 · SQLAlchemy 2 · ytmusicapi · RapidFuzz (declared, used later) · pytest · ruff

**Repository layout produced by this plan:**

```
like-surgeon/
├── pyproject.toml
├── README.md
├── .gitignore
├── LICENSE                       # already present
├── docs/superpowers/plans/       # this plan
├── src/likesurgeon/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── models.py                 # SQLAlchemy ORM
│   ├── dtos.py                   # Pydantic DTOs
│   ├── normalize.py
│   ├── ytmusic_client.py
│   ├── snapshot.py
│   ├── diff.py
│   ├── export.py
│   └── doctor.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_normalize.py
    ├── test_snapshot.py
    ├── test_diff.py
    ├── test_export.py
    └── test_ytmusic_client.py
```

> Note: spec lists `models.py` only. We split Pydantic DTOs into `dtos.py` because the ORM and validation models have different lifecycles and would otherwise create circular concerns. `models.py` = persistence; `dtos.py` = transport.

**Commit policy (project-wide rule from `~/.claude/CLAUDE.md`):** Each task is one turn of *work + local verification*. **Do not run the commit command in the same turn as the work.** End the turn after verification passes so the stop-time review hook can inspect the diff. On the next turn — before starting the next task — run the suggested `git add` / `git commit` from the previous task's final step. The plan still lists exact commit commands for clarity, but they belong on the *following* turn.

**Auth scope for MVP 0.1:** YouTube Music auth uses the **browser-header** flow only (`ytmusicapi browser`). OAuth is deferred to a later milestone because ytmusicapi ≥ 1.7 requires a user-supplied Google Cloud client id / secret and an `OAuthCredentials` instance (see [ytmusicapi OAuth docs](https://ytmusicapi.readthedocs.io/en/latest/setup/oauth.html)) — extra UX surface that this MVP intentionally avoids.

---

## Task 1: Bootstrap project (pyproject, .gitignore, uv sync)

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/likesurgeon/__init__.py`
- Create: `tests/__init__.py`
- Create: `README.md` (minimal stub — Task 11 overwrites it with the real one)

> Note: `tests/conftest.py` is created in Task 4 — its fixture imports from
> `likesurgeon.db`, which doesn't exist yet. Adding it earlier would break
> `pytest` collection.

> **Why a stub README now?** `pyproject.toml` declares `readme = "README.md"`,
> and hatchling's editable build (which `uv sync` performs) validates that
> file exists *at sync time* and aborts with `OSError: Readme file does not
> exist: README.md`. So we ship a minimal stub here and Task 11 replaces it
> with real content.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "likesurgeon"
version = "0.1.0"
description = "Sync, backup, and repair your YouTube Music liked songs."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "zeikar" }]
dependencies = [
  "ytmusicapi>=1.7",
  "typer>=0.12",
  "rich>=13.7",
  "pydantic>=2.7",
  "rapidfuzz>=3.6",
  "sqlalchemy>=2.0",
]

[project.scripts]
likesurgeon = "likesurgeon.cli:app"

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "ruff>=0.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/likesurgeon"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.eggs/

# Virtualenv / uv
.venv/
.python-version

# Tooling
.ruff_cache/
.pytest_cache/
.mypy_cache/
.coverage
htmlcov/

# Editor
.idea/
.vscode/
*.swp
.DS_Store

# Local app data (in case anyone runs `likesurgeon init` from inside the repo)
.like-surgeon/
*.sqlite
*.sqlite-journal
browser.json
# Not used in 0.1; ignored defensively for any future hand-placed OAuth file.
ytmusic-oauth.json
```

- [ ] **Step 3: Write `src/likesurgeon/__init__.py`**

```python
"""like-surgeon: sync, backup, and repair your YouTube Music liked songs."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Write `tests/__init__.py`**

Empty file:

```python
```

- [ ] **Step 5: Write `README.md` stub**

Minimal placeholder so hatchling's editable build doesn't fail at `uv sync`.
Task 11 overwrites this with the real README.

```markdown
# like-surgeon

Sync, backup, and repair your YouTube Music liked songs.
```

- [ ] **Step 6: Sync deps with uv**

Run: `uv sync --extra dev`
Expected: `Installed N packages` (no errors). Creates `.venv/` and `uv.lock`.

- [ ] **Step 7: Verify ruff passes on the empty package**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` (formatter may report 0 files reformatted).

- [ ] **Step 8: Verify the package imports**

Run: `uv run python -c "import likesurgeon; print(likesurgeon.__version__)"`
Expected: `0.1.0`

(We skip running pytest here — it would exit 5 with no tests, which automated runners often treat as failure. First pytest invocation is in Task 3 once the normalize tests exist.)

- [ ] **Step 9: End turn — commit on next turn**

```bash
git add pyproject.toml .gitignore src/likesurgeon/__init__.py tests/__init__.py README.md uv.lock
git commit -m "chore: bootstrap likesurgeon Python package with uv"
```

---

## Task 2: Config module

**Files:**
- Create: `src/likesurgeon/config.py`

- [ ] **Step 1: Write `src/likesurgeon/config.py`**

```python
"""Local app configuration: paths, env overrides, on-disk layout."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_DIR_NAME = ".like-surgeon"
DEFAULT_DB_FILENAME = "like-surgeon.sqlite"
YTMUSIC_BROWSER_FILENAME = "browser.json"
ENV_HOME = "LIKE_SURGEON_HOME"


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 2: Lint check**

Run: `uv run ruff check src/likesurgeon/config.py`
Expected: `All checks passed!`

- [ ] **Step 3: Smoke-test resolution**

Run:
```bash
uv run python -c "import os; os.environ['LIKE_SURGEON_HOME']='/tmp/ls-test'; from likesurgeon.config import Config; c=Config.load(); print(c.app_dir); print(c.db_path)"
```
Expected output:
```
/tmp/ls-test
/tmp/ls-test/like-surgeon.sqlite
```

- [ ] **Step 4: End turn — commit on next turn**

```bash
git add src/likesurgeon/config.py
git commit -m "feat(config): resolve app dir and DB paths with env override"
```

---

## Task 3: Normalization module (TDD)

**Files:**
- Create: `tests/test_normalize.py`
- Create: `src/likesurgeon/normalize.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_normalize.py`:

```python
from likesurgeon.normalize import canonical_key, normalize_artists, normalize_title


def test_normalize_title_strips_official_music_video():
    assert normalize_title("Song Name (Official Music Video)") == "song name"


def test_normalize_title_strips_official_audio():
    assert normalize_title("Song Name [Official Audio]") == "song name"


def test_normalize_title_strips_lyrics_bracket():
    assert normalize_title("Song Name (Lyrics)") == "song name"


def test_normalize_title_strips_mv_suffix():
    assert normalize_title("Song Name - MV") == "song name"


def test_normalize_title_strips_visualizer():
    assert normalize_title("Song Name (Visualizer)") == "song name"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("  Song   Name  ") == "song name"


def test_normalize_title_preserves_non_noise_brackets():
    # Year tag is not a noise word — keep it.
    assert normalize_title("Song Name (2024)") == "song name (2024)"


def test_normalize_artists_lowercases_and_sorts():
    assert normalize_artists(["BTS", "Coldplay"]) == "bts, coldplay"
    assert normalize_artists(["Coldplay", "BTS"]) == "bts, coldplay"


def test_normalize_artists_drops_empty():
    assert normalize_artists(["", "  ", "Artist"]) == "artist"


def test_canonical_key_matches_across_decoration():
    a = canonical_key("Song (Official Audio)", ["Artist"])
    b = canonical_key("song", ["artist"])
    assert a == b


def test_canonical_key_distinguishes_different_songs():
    a = canonical_key("Song A", ["Artist"])
    b = canonical_key("Song B", ["Artist"])
    assert a != b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: all tests fail with `ModuleNotFoundError: No module named 'likesurgeon.normalize'`.

- [ ] **Step 3: Implement `src/likesurgeon/normalize.py`**

```python
"""Title/artist normalization to compute canonical match keys.

Pure-functional. No I/O. Used by snapshot ingestion and (later) the matching
engine in MVP 0.3.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Noise patterns that decorate titles but don't change song identity.
# Match as whole words; case folding happens before the regex runs.
_NOISE_PATTERNS: tuple[str, ...] = (
    r"\bofficial\s+music\s+video\b",
    r"\bofficial\s+mv\b",
    r"\bofficial\s+video\b",
    r"\bofficial\s+audio\b",
    r"\blyric\s+video\b",
    r"\blyrics\b",
    r"\bvisualizer\b",
    r"\baudio\b",
    r"\bm/v\b",
    r"\bmv\b",
)
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))
_BRACKET_RE = re.compile(r"[\(\[\{][^()\[\]\{\}]*[\)\]\}]")
_WS_RE = re.compile(r"\s+")
_TRIM_CHARS = " -–—\t"


def _strip_noise_brackets(text: str) -> str:
    """Remove bracketed segments that contain a noise word; keep the rest."""

    def _replace(match: re.Match[str]) -> str:
        return " " if _NOISE_RE.search(match.group(0)) else match.group(0)

    return _BRACKET_RE.sub(_replace, text)


def normalize_title(title: str) -> str:
    """Lowercase, strip noise decorations, collapse whitespace."""
    s = title.lower()
    s = _strip_noise_brackets(s)
    s = _NOISE_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip(_TRIM_CHARS)
    return s


def normalize_artists(artists: Iterable[str]) -> str:
    """Lowercase, trim, drop empties, sort, comma-join.

    Sorting makes the key order-independent so collaborations match
    regardless of which artist YouTube lists first.
    """
    parts = [_WS_RE.sub(" ", a.lower().strip()) for a in artists if a is not None]
    parts = [p for p in parts if p]
    parts.sort()
    return ", ".join(parts)


def canonical_key(title: str, artists: Iterable[str]) -> str:
    """Deterministic identity key: ``"<artists>|<title>"``."""
    return f"{normalize_artists(artists)}|{normalize_title(title)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalize.py -v`
Expected: all 11 tests pass.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/likesurgeon/normalize.py tests/test_normalize.py`
Expected: `All checks passed!`

- [ ] **Step 6: End turn — commit on next turn**

```bash
git add src/likesurgeon/normalize.py tests/test_normalize.py
git commit -m "feat(normalize): canonical-key generation with noise-word stripping"
```

---

## Task 4: ORM models + DB helpers + shared test fixture

**Files:**
- Create: `src/likesurgeon/models.py`
- Create: `src/likesurgeon/db.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `src/likesurgeon/models.py`**

```python
"""SQLAlchemy ORM models for snapshots, tracks, and snapshot membership."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Project-wide declarative base."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Track(Base):
    """A normalized, deduplicated track record.

    Identity (``source``, ``dedupe_key``):
      - ``dedupe_key`` is the provider's video id when present.
      - Otherwise it falls back to ``"canon:<canonical_key>"``.
    This lets us merge re-likes of the same video and still group cross-source
    matches by canonical key when video ids are missing.
    """

    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    artists: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnails_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_key: Mapped[str] = mapped_column(Text, index=True)
    dedupe_key: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("source", "dedupe_key", name="uq_track_source_dedupe"),
    )


class Snapshot(Base):
    """A point-in-time capture of liked songs from a single source."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    raw_count: Mapped[int] = mapped_column(Integer)

    items: Mapped[list[SnapshotItem]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class SnapshotItem(Base):
    """Membership row linking a snapshot to a track at a given position.

    **Point-in-time metadata.** ``Track`` holds the *latest* known metadata
    for an identity (and gets overwritten on each scan). The columns below
    capture the values seen *at the time this snapshot was taken*, so that
    exporting an old snapshot returns the metadata from when it was scanned —
    even if the same video has since been renamed, re-credited, or had its
    raw payload reshaped by ytmusicapi. This is the whole point of "backup".
    """

    __tablename__ = "snapshot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="RESTRICT"))
    position: Mapped[int] = mapped_column(Integer)

    # Point-in-time captured metadata (frozen at scan time).
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    artists: Mapped[str] = mapped_column(Text)  # JSON-encoded list[str]
    album: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnails_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_key: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    snapshot: Mapped[Snapshot] = relationship(back_populates="items")
    track: Mapped[Track] = relationship()

    __table_args__ = (
        # We deliberately do NOT constrain ``(snapshot_id, track_id)``: a scan
        # that returns the same dedupe_key twice (rare — only when ``video_id``
        # is absent and two items share a ``canonical_key``) should still be
        # recorded faithfully rather than failing with IntegrityError, since
        # the whole point of a snapshot is to preserve what was scanned.
        # Position uniqueness within a snapshot is enough to keep ordering
        # deterministic and also covers ordered-read queries.
        UniqueConstraint("snapshot_id", "position", name="uq_snapitem_snap_pos"),
    )
```

- [ ] **Step 2: Write `src/likesurgeon/db.py`**

```python
"""SQLAlchemy engine, session factory, and schema initializer."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(db_path: Path) -> Engine:
    """Build a SQLite engine pointing at ``db_path``."""
    url = f"sqlite:///{db_path}"
    return create_engine(url, future=True)


def init_db(engine: Engine) -> None:
    """Create all tables if they don't already exist."""
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
```

- [ ] **Step 3: Write `tests/conftest.py`**

```python
"""Shared pytest fixtures."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from likesurgeon.db import init_db, make_session_factory


@pytest.fixture
def session() -> Iterator[Session]:
    """In-memory SQLite session, schema initialized."""
    engine = create_engine("sqlite:///:memory:", future=True)
    init_db(engine)
    factory = make_session_factory(engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
```

- [ ] **Step 4: Smoke-test schema creation in-memory**

Run:
```bash
uv run python -c "
from sqlalchemy import create_engine, inspect
from likesurgeon.db import init_db
e = create_engine('sqlite:///:memory:', future=True)
init_db(e)
print(sorted(inspect(e).get_table_names()))
"
```
Expected: `['snapshot_items', 'snapshots', 'tracks']`

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/likesurgeon/models.py src/likesurgeon/db.py tests/conftest.py`
Expected: `All checks passed!`

- [ ] **Step 6: Run pytest to confirm conftest collection works**

Run: `uv run pytest`
Expected: existing normalize tests still pass; no collection errors from `conftest.py`.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/models.py src/likesurgeon/db.py tests/conftest.py
git commit -m "feat(db): SQLAlchemy schema and shared in-memory test fixture"
```

---

## Task 5: Pydantic DTOs

**Files:**
- Create: `src/likesurgeon/dtos.py`

- [ ] **Step 1: Write `src/likesurgeon/dtos.py`**

```python
"""Pydantic DTOs for serializing tracks/snapshots over CLI/JSON boundaries."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import Snapshot, SnapshotItem, Track


class TrackDTO(BaseModel):
    """JSON-serializable view of a track.

    Constructed from either:
      * ``from_orm_track`` — *latest* known metadata (master row).
      * ``from_snapshot_item`` — *point-in-time* metadata (snapshot row), with
        ``first_seen_at``/``last_seen_at`` filled from the linked master Track.
        Use this for export so old snapshots return their original metadata.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    video_id: str | None
    title: str
    artists: list[str]
    album: str | None
    duration_seconds: int | None
    thumbnails: list[dict[str, Any]] | None
    canonical_key: str
    first_seen_at: datetime
    last_seen_at: datetime

    @classmethod
    def from_orm_track(cls, track: Track) -> TrackDTO:
        return cls(
            id=track.id,
            source=track.source,
            video_id=track.video_id,
            title=track.title,
            artists=json.loads(track.artists) if track.artists else [],
            album=track.album,
            duration_seconds=track.duration_seconds,
            thumbnails=json.loads(track.thumbnails_json) if track.thumbnails_json else None,
            canonical_key=track.canonical_key,
            first_seen_at=track.first_seen_at,
            last_seen_at=track.last_seen_at,
        )

    @classmethod
    def from_snapshot_item(cls, item: SnapshotItem, source: str) -> TrackDTO:
        """Build a DTO from a snapshot's point-in-time captured metadata.

        ``source`` is taken from the parent ``Snapshot`` (callers already have
        it, so we don't pay an extra DB lookup). ``first_seen_at`` /
        ``last_seen_at`` come from the linked ``Track`` row — eager-load via
        ``selectinload(SnapshotItem.track)`` to avoid N+1 queries.
        """
        return cls(
            id=item.track_id,
            source=source,
            video_id=item.video_id,
            title=item.title,
            artists=json.loads(item.artists) if item.artists else [],
            album=item.album,
            duration_seconds=item.duration_seconds,
            thumbnails=json.loads(item.thumbnails_json) if item.thumbnails_json else None,
            canonical_key=item.canonical_key,
            first_seen_at=item.track.first_seen_at,
            last_seen_at=item.track.last_seen_at,
        )


class SnapshotDTO(BaseModel):
    """JSON-serializable view of a snapshot row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    created_at: datetime
    raw_count: int

    @classmethod
    def from_orm_snapshot(cls, snapshot: Snapshot) -> SnapshotDTO:
        return cls(
            id=snapshot.id,
            source=snapshot.source,
            created_at=snapshot.created_at,
            raw_count=snapshot.raw_count,
        )
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/likesurgeon/dtos.py`
Expected: `All checks passed!`

- [ ] **Step 3: End turn — commit on next turn**

```bash
git add src/likesurgeon/dtos.py
git commit -m "feat(dtos): Pydantic transport models for tracks and snapshots"
```

---

## Task 6: Snapshot service (ingest, list, retrieve) + duplicate-track regression test

**Files:**
- Create: `src/likesurgeon/snapshot.py`
- Create: `tests/test_snapshot.py`

- [ ] **Step 1: Write `src/likesurgeon/snapshot.py`**

```python
"""Snapshot ingestion and retrieval.

This module is the only writer of ``Track`` and ``Snapshot`` rows. Callers
hand it raw provider dicts (e.g. ytmusicapi output); it normalizes, upserts
tracks by ``(source, dedupe_key)``, and records snapshot membership.

**Point-in-time guarantee.** ``Track`` holds the *latest* known metadata
(overwritten on every scan), but ``SnapshotItem`` freezes the values seen at
scan time. Reading a snapshot back via ``get_snapshot_items`` therefore
returns the metadata that was captured then, not whatever the master Track
row currently says.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Snapshot, SnapshotItem, Track
from .normalize import canonical_key


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_duration(text: str) -> int | None:
    """Parse ``M:SS`` or ``H:MM:SS`` into seconds; ``None`` if unparseable."""
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _coerce_artists(raw: Any) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                out.append(str(name))
        elif entry:
            out.append(str(entry))
    return out


def _ytmusic_to_record(source: str, item: dict[str, Any]) -> dict[str, Any]:
    """Translate one provider dict to a normalized track record."""
    video_id = item.get("videoId")
    title = str(item.get("title") or "")
    artists = _coerce_artists(item.get("artists"))
    album_obj = item.get("album")
    album = album_obj.get("name") if isinstance(album_obj, dict) else None

    duration_seconds = item.get("duration_seconds")
    if duration_seconds is None and item.get("duration"):
        duration_seconds = _parse_duration(str(item["duration"]))

    canon = canonical_key(title, artists)
    dedupe = video_id or f"canon:{canon}"

    return {
        "source": source,
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": album,
        "duration_seconds": duration_seconds,
        "thumbnails": item.get("thumbnails"),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": item,
    }


def _upsert_track(session: Session, source: str, rec: dict[str, Any]) -> Track:
    stmt = select(Track).where(
        Track.source == source, Track.dedupe_key == rec["dedupe_key"]
    )
    track = session.scalar(stmt)
    now = _utcnow()
    if track is None:
        track = Track(
            source=source,
            video_id=rec["video_id"],
            title=rec["title"],
            artists=json.dumps(rec["artists"], ensure_ascii=False),
            album=rec["album"],
            duration_seconds=rec["duration_seconds"],
            thumbnails_json=(
                json.dumps(rec["thumbnails"], ensure_ascii=False)
                if rec["thumbnails"]
                else None
            ),
            canonical_key=rec["canonical_key"],
            dedupe_key=rec["dedupe_key"],
            raw_json=json.dumps(rec["raw"], ensure_ascii=False),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(track)
        session.flush()
        return track

    track.title = rec["title"]
    track.artists = json.dumps(rec["artists"], ensure_ascii=False)
    track.album = rec["album"]
    track.duration_seconds = rec["duration_seconds"]
    track.thumbnails_json = (
        json.dumps(rec["thumbnails"], ensure_ascii=False) if rec["thumbnails"] else None
    )
    track.canonical_key = rec["canonical_key"]
    track.raw_json = json.dumps(rec["raw"], ensure_ascii=False)
    track.last_seen_at = now
    return track


def create_snapshot(
    session: Session, source: str, items: Iterable[dict[str, Any]]
) -> Snapshot:
    """Persist a snapshot of ``items`` from ``source``. Returns the snapshot row."""
    items_list = list(items)
    snapshot = Snapshot(source=source, created_at=_utcnow(), raw_count=len(items_list))
    session.add(snapshot)
    session.flush()
    for position, item in enumerate(items_list):
        rec = _ytmusic_to_record(source, item)
        track = _upsert_track(session, source, rec)
        session.add(
            SnapshotItem(
                snapshot_id=snapshot.id,
                track_id=track.id,
                position=position,
                # Freeze the values seen at scan time so future Track upserts
                # don't rewrite this snapshot's metadata.
                video_id=rec["video_id"],
                title=rec["title"],
                artists=json.dumps(rec["artists"], ensure_ascii=False),
                album=rec["album"],
                duration_seconds=rec["duration_seconds"],
                thumbnails_json=(
                    json.dumps(rec["thumbnails"], ensure_ascii=False)
                    if rec["thumbnails"]
                    else None
                ),
                canonical_key=rec["canonical_key"],
                raw_json=json.dumps(rec["raw"], ensure_ascii=False),
            )
        )
    session.flush()
    return snapshot


def list_snapshots(session: Session) -> list[Snapshot]:
    stmt = select(Snapshot).order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
    return list(session.scalars(stmt).all())


def get_snapshot(session: Session, snapshot_id: int) -> Snapshot | None:
    return session.get(Snapshot, snapshot_id)


def get_snapshot_items(session: Session, snapshot_id: int) -> list[SnapshotItem]:
    """Return point-in-time snapshot rows in scan order.

    Eager-loads the linked master ``Track`` so DTO conversion can read
    ``first_seen_at`` / ``last_seen_at`` without N+1 queries.
    """
    stmt = (
        select(SnapshotItem)
        .where(SnapshotItem.snapshot_id == snapshot_id)
        .order_by(SnapshotItem.position)
        .options(selectinload(SnapshotItem.track))
    )
    return list(session.scalars(stmt).all())


def latest_snapshot(session: Session, source: str | None = None) -> Snapshot | None:
    stmt = select(Snapshot)
    if source is not None:
        stmt = stmt.where(Snapshot.source == source)
    stmt = stmt.order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1)
    return session.scalar(stmt)
```

- [ ] **Step 2: Smoke-test ingestion**

Run:
```bash
uv run python -c "
from sqlalchemy import create_engine
from likesurgeon.db import init_db, make_session_factory, session_scope
from likesurgeon.snapshot import create_snapshot, get_snapshot_items
e = create_engine('sqlite:///:memory:', future=True); init_db(e)
factory = make_session_factory(e)
with session_scope(factory) as s:
    snap = create_snapshot(s, 'ytmusic', [
        {'videoId':'v1','title':'A (Official MV)','artists':[{'name':'X'}]},
        {'videoId':'v2','title':'B','artists':[{'name':'Y'}],'duration':'3:21'},
    ])
    print('snap', snap.id, 'count', snap.raw_count)
    for it in get_snapshot_items(s, snap.id):
        print(it.track_id, it.title, '|', it.canonical_key, '|', it.duration_seconds)
"
```
Expected:
```
snap 1 count 2
1 A (Official MV) | x|a | None
2 B | y|b | 201
```
(The stored title keeps its original casing; `canonical_key` is normalized; the first item has no `duration`/`duration_seconds` field, so it stores `None`. Note we read from `SnapshotItem` columns — the point-in-time copy — not from `Track`.)

- [ ] **Step 3: Write `tests/test_snapshot.py` (duplicate-track regression)**

Locks in the `(snapshot_id, position)`-only uniqueness policy: a scan that
returns the same identity twice must be recorded faithfully (both rows present,
both pointing at the same upserted master track), not rejected with
`IntegrityError`.

```python
"""Tests for snapshot ingestion policy.

Pinned: a snapshot must record duplicate identities faithfully — that's the
whole reason ``SnapshotItem`` only has a unique constraint on
``(snapshot_id, position)`` and not on ``(snapshot_id, track_id)``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.snapshot import create_snapshot, get_snapshot_items


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_create_snapshot_allows_duplicate_track_within_snapshot(session: Session):
    """Two identical items in one scan → both stored, sharing one master track."""
    duplicated = _item("v1", "Same Song", ["A"])
    snap = create_snapshot(session, "ytmusic", [duplicated, duplicated])
    session.commit()

    assert snap.raw_count == 2
    items = get_snapshot_items(session, snap.id)
    assert len(items) == 2
    # Both items reference the same upserted master track row.
    assert items[0].track_id == items[1].track_id
    assert {it.position for it in items} == {0, 1}
    # Point-in-time metadata is captured on each row independently.
    assert all(it.title == "Same Song" for it in items)
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/test_snapshot.py -v`
Expected: 1 passed.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/likesurgeon/snapshot.py tests/test_snapshot.py`
Expected: `All checks passed!`

- [ ] **Step 6: End turn — commit on next turn**

```bash
git add src/likesurgeon/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): ingest with point-in-time capture; allow duplicate identities"
```

---

## Task 7: Diff service (TDD)

**Files:**
- Create: `tests/test_diff.py`
- Create: `src/likesurgeon/diff.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_diff.py`:

```python
import pytest
from sqlalchemy.orm import Session

from likesurgeon.diff import diff_snapshots
from likesurgeon.snapshot import create_snapshot


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_diff_detects_added_and_removed(session: Session):
    a = _item("v1", "Song A", ["X"])
    b = _item("v2", "Song B", ["Y"])
    c = _item("v3", "Song C", ["Z"])

    old = create_snapshot(session, "ytmusic", [a, b])
    session.commit()
    new = create_snapshot(session, "ytmusic", [b, c])
    session.commit()

    result = diff_snapshots(session, old.id, new.id)
    assert {t.title for t in result.added} == {"Song C"}
    assert {t.title for t in result.removed} == {"Song A"}
    assert result.common_count == 1


def test_diff_identical_snapshots_yields_no_changes(session: Session):
    a = _item("v1", "Song A", ["X"])
    s1 = create_snapshot(session, "ytmusic", [a])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [a])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert result.added == []
    assert result.removed == []
    assert result.common_count == 1


def test_diff_unknown_snapshot_raises(session: Session):
    with pytest.raises(ValueError):
        diff_snapshots(session, 999, 1000)


def test_diff_falls_back_to_canonical_key_when_video_id_missing(session: Session):
    # Same song, different decoration, no video id → upsert should merge them.
    a = {"title": "Song (Official Audio)", "artists": [{"name": "X"}]}
    b = {"title": "song", "artists": [{"name": "x"}]}
    s1 = create_snapshot(session, "ytmusic", [a])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [b])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert result.added == []
    assert result.removed == []
    assert result.common_count == 1


def test_diff_uses_point_in_time_metadata(session: Session):
    """The Removed table should show the title from the snapshot, not the
    latest Track row — even after the same video is re-liked under a new
    title (which overwrites Track.title via upsert).
    """
    s1 = create_snapshot(session, "ytmusic", [_item("v1", "Old Title", ["A"])])
    session.commit()
    s2 = create_snapshot(session, "ytmusic", [])
    session.commit()
    # Re-like the same video with a renamed title — this upsert overwrites
    # the master Track row's title to "New Title".
    create_snapshot(session, "ytmusic", [_item("v1", "New Title", ["A"])])
    session.commit()

    result = diff_snapshots(session, s1.id, s2.id)
    assert {t.title for t in result.removed} == {"Old Title"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_diff.py -v`
Expected: tests fail with `ModuleNotFoundError: No module named 'likesurgeon.diff'`.

- [ ] **Step 3: Implement `src/likesurgeon/diff.py`**

```python
"""Snapshot diffing.

Compares two snapshots and reports which tracks were added, removed, and
shared. Identity is based on ``SnapshotItem.track_id`` — two snapshots that
reference the same master track row are considered to share that track
regardless of position. Display fields (title, artists, …) come from each
snapshot's *point-in-time* row, so the "Removed" table shows the metadata
the user saw in the old snapshot — not whatever the master Track currently
holds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import SnapshotItem
from .snapshot import get_snapshot, get_snapshot_items


@dataclass(frozen=True)
class TrackSummary:
    """Light view of a snapshot item for diff output."""

    track_id: int
    title: str
    artists: list[str]
    canonical_key: str
    video_id: str | None


@dataclass(frozen=True)
class DiffResult:
    added: list[TrackSummary]
    removed: list[TrackSummary]
    common_count: int


def _summarize(item: SnapshotItem) -> TrackSummary:
    return TrackSummary(
        track_id=item.track_id,
        title=item.title,
        artists=json.loads(item.artists) if item.artists else [],
        canonical_key=item.canonical_key,
        video_id=item.video_id,
    )


def diff_snapshots(session: Session, old_id: int, new_id: int) -> DiffResult:
    """Diff two snapshots by master-track identity, using point-in-time metadata for display."""
    if get_snapshot(session, old_id) is None:
        raise ValueError(f"Snapshot {old_id} not found")
    if get_snapshot(session, new_id) is None:
        raise ValueError(f"Snapshot {new_id} not found")

    old_items = {it.track_id: it for it in get_snapshot_items(session, old_id)}
    new_items = {it.track_id: it for it in get_snapshot_items(session, new_id)}

    added = [_summarize(it) for tid, it in new_items.items() if tid not in old_items]
    removed = [_summarize(it) for tid, it in old_items.items() if tid not in new_items]
    common = len(set(old_items) & set(new_items))
    return DiffResult(added=added, removed=removed, common_count=common)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_diff.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: 17 passed (11 normalize + 1 snapshot + 5 diff).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/diff.py tests/test_diff.py`
Expected: `All checks passed!`

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/diff.py tests/test_diff.py
git commit -m "feat(diff): compare snapshots by track identity, with tests"
```

---

## Task 8: Export & Doctor services + export tests

**Files:**
- Create: `src/likesurgeon/export.py`
- Create: `src/likesurgeon/doctor.py`
- Create: `tests/test_export.py`

- [ ] **Step 1: Write `src/likesurgeon/export.py`**

```python
"""Snapshot export.

MVP 0.1 supports JSON only. Future formats (CSV, M3U) will plug in as
additional functions; the CLI dispatches by ``--format``.

Reads from ``SnapshotItem`` (point-in-time metadata) — exporting an old
snapshot returns the metadata captured at scan time, not the master Track's
current values.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .dtos import SnapshotDTO, TrackDTO
from .snapshot import get_snapshot, get_snapshot_items


def export_snapshot_json(session: Session, snapshot_id: int) -> str:
    """Return a pretty-printed JSON string for the snapshot."""
    snapshot = get_snapshot(session, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Snapshot {snapshot_id} not found")

    items = get_snapshot_items(session, snapshot_id)
    payload = {
        "snapshot": SnapshotDTO.from_orm_snapshot(snapshot).model_dump(mode="json"),
        "tracks": [
            TrackDTO.from_snapshot_item(item, source=snapshot.source).model_dump(mode="json")
            for item in items
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
```

- [ ] **Step 2: Write `src/likesurgeon/doctor.py`**

```python
"""Health summary across stored snapshots.

Read-only. No external calls. Used by ``likesurgeon doctor``.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .diff import DiffResult, diff_snapshots
from .models import Snapshot
from .snapshot import latest_snapshot


@dataclass(frozen=True)
class HealthReport:
    total_snapshots: int
    latest_source: str | None
    latest_count: int | None
    last_diff: DiffResult | None


def health_summary(session: Session, source: str = "ytmusic") -> HealthReport:
    total = session.scalar(select(func.count(Snapshot.id))) or 0
    latest = latest_snapshot(session, source=source)
    if latest is None:
        return HealthReport(
            total_snapshots=total,
            latest_source=None,
            latest_count=None,
            last_diff=None,
        )

    # raw_count == # of SnapshotItem rows by construction (one per scan item).
    count = latest.raw_count

    prev_stmt = (
        select(Snapshot)
        .where(Snapshot.source == source, Snapshot.id != latest.id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    prev = session.scalar(prev_stmt)
    diff = diff_snapshots(session, prev.id, latest.id) if prev is not None else None

    return HealthReport(
        total_snapshots=total,
        latest_source=latest.source,
        latest_count=count,
        last_diff=diff,
    )
```

- [ ] **Step 3: Write `tests/test_export.py`**

```python
"""Tests for snapshot export — including the point-in-time guarantee."""
from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from likesurgeon.export import export_snapshot_json
from likesurgeon.snapshot import create_snapshot


def _item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def test_export_returns_snapshot_and_tracks(session: Session):
    snap = create_snapshot(session, "ytmusic", [_item("v1", "A", ["X"])])
    session.commit()

    payload = json.loads(export_snapshot_json(session, snap.id))
    assert payload["snapshot"]["id"] == snap.id
    assert payload["snapshot"]["raw_count"] == 1
    assert payload["snapshot"]["source"] == "ytmusic"
    assert len(payload["tracks"]) == 1
    assert payload["tracks"][0]["title"] == "A"
    assert payload["tracks"][0]["video_id"] == "v1"
    assert payload["tracks"][0]["artists"] == ["X"]


def test_export_preserves_point_in_time_metadata(session: Session):
    """The whole reason the SnapshotItem table carries metadata: an old
    snapshot's exported title must not change just because the same video
    was later re-liked under a new title.
    """
    s_old = create_snapshot(session, "ytmusic", [_item("v1", "Old Title", ["A"])])
    session.commit()
    # Same video re-liked with renamed metadata — the upsert overwrites
    # Track.title to "New Title". SnapshotItem of s_old must not move.
    s_new = create_snapshot(session, "ytmusic", [_item("v1", "New Title", ["A"])])
    session.commit()

    old_payload = json.loads(export_snapshot_json(session, s_old.id))
    new_payload = json.loads(export_snapshot_json(session, s_new.id))
    assert old_payload["tracks"][0]["title"] == "Old Title"
    assert new_payload["tracks"][0]["title"] == "New Title"


def test_export_unknown_snapshot_raises(session: Session):
    with pytest.raises(ValueError):
        export_snapshot_json(session, 999)
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/test_export.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: 20 passed (11 normalize + 1 snapshot + 5 diff + 3 export).

- [ ] **Step 6: Smoke-test doctor (no test added — covered indirectly via diff/export tests)**

Run:
```bash
uv run python -c "
from sqlalchemy import create_engine
from likesurgeon.db import init_db, make_session_factory, session_scope
from likesurgeon.snapshot import create_snapshot
from likesurgeon.doctor import health_summary
e = create_engine('sqlite:///:memory:', future=True); init_db(e)
factory = make_session_factory(e)
with session_scope(factory) as s:
    create_snapshot(s, 'ytmusic', [{'videoId':'v1','title':'A','artists':[{'name':'X'}]}])
    r = health_summary(s)
    print('total', r.total_snapshots, 'latest', r.latest_count)
"
```
Expected: `total 1 latest 1`.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/likesurgeon/export.py src/likesurgeon/doctor.py tests/test_export.py`
Expected: `All checks passed!`

- [ ] **Step 8: End turn — commit on next turn**

```bash
git add src/likesurgeon/export.py src/likesurgeon/doctor.py tests/test_export.py
git commit -m "feat(export,doctor): point-in-time JSON export and health summary"
```

---

## Task 9: ytmusicapi client wrapper (browser auth) + tests

**Files:**
- Create: `src/likesurgeon/ytmusic_client.py`
- Create: `tests/test_ytmusic_client.py`

- [ ] **Step 1: Write `src/likesurgeon/ytmusic_client.py`**

```python
"""Thin wrapper around ``ytmusicapi`` for testability and clear errors.

MVP 0.1 supports the **browser-header** auth flow only. OAuth is deferred —
ytmusicapi >= 1.7 requires user-supplied Google Cloud credentials wrapped in
``OAuthCredentials``, which is more UX surface than this MVP wants to expose.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic


class AuthFileMissingError(FileNotFoundError):
    """Raised when no usable auth file exists yet."""


class YTMusicClient:
    """Resolves the browser-header auth file and proxies calls.

    The wrapper exists so that:
      * tests can subclass and override ``_build`` to inject a fake YTMusic.
      * the CLI sees one clear exception when auth isn't set up yet, instead
        of leaking ytmusicapi internals.
    """

    def __init__(self, browser_path: Path | None = None) -> None:
        self._browser_path = browser_path

    def _build(self) -> YTMusic:
        if self._browser_path is not None and self._browser_path.exists():
            return YTMusic(str(self._browser_path))
        raise AuthFileMissingError(
            "No YouTube Music auth file found. "
            "Run `likesurgeon auth ytmusic` and follow the printed instructions."
        )

    def fetch_liked_songs(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` liked songs. Returns the raw track dicts."""
        client = self._build()
        result = client.get_liked_songs(limit=limit)
        if isinstance(result, dict):
            tracks = result.get("tracks", [])
            return list(tracks) if isinstance(tracks, list) else []
        return []
```

- [ ] **Step 2: Write `tests/test_ytmusic_client.py`**

```python
"""Tests for YTMusicClient — fake ytmusicapi via subclass."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from likesurgeon.ytmusic_client import AuthFileMissingError, YTMusicClient


class _FakeYTMusic:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.last_limit: int | None = None

    def get_liked_songs(self, limit: int) -> Any:
        self.last_limit = limit
        return self._payload


class _FakeClient(YTMusicClient):
    """YTMusicClient that bypasses auth and returns a canned payload."""

    def __init__(self, payload: Any) -> None:
        super().__init__(browser_path=None)
        self._payload = payload
        self.fake: _FakeYTMusic | None = None

    def _build(self) -> Any:
        self.fake = _FakeYTMusic(self._payload)
        return self.fake


def test_fetch_liked_songs_returns_track_list():
    payload = {"tracks": [{"videoId": "v1", "title": "A", "artists": []}]}
    client = _FakeClient(payload)
    result = client.fetch_liked_songs(limit=42)
    assert len(result) == 1
    assert result[0]["videoId"] == "v1"
    assert client.fake is not None
    assert client.fake.last_limit == 42


def test_fetch_liked_songs_handles_missing_tracks_key():
    client = _FakeClient({})
    assert client.fetch_liked_songs() == []


def test_fetch_liked_songs_handles_non_dict_response():
    client = _FakeClient([])
    assert client.fetch_liked_songs() == []


def test_missing_auth_raises():
    client = YTMusicClient(browser_path=Path("/nope/browser.json"))
    with pytest.raises(AuthFileMissingError):
        client.fetch_liked_songs()
```

- [ ] **Step 3: Run the new tests**

Run: `uv run pytest tests/test_ytmusic_client.py -v`
Expected: 4 passed.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: 24 passed (11 normalize + 1 snapshot + 5 diff + 3 export + 4 ytmusic_client).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/likesurgeon/ytmusic_client.py tests/test_ytmusic_client.py`
Expected: `All checks passed!`

- [ ] **Step 6: End turn — commit on next turn**

```bash
git add src/likesurgeon/ytmusic_client.py tests/test_ytmusic_client.py
git commit -m "feat(ytmusic): browser-auth wrapper with clear missing-auth error and tests"
```

---

## Task 10: CLI scaffold + `init`, `auth ytmusic`, `scan ytmusic`

**Files:**
- Create: `src/likesurgeon/cli.py`

- [ ] **Step 1: Write `src/likesurgeon/cli.py`**

```python
"""Typer CLI for likesurgeon. Read-only except for local DB writes."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import sessionmaker

from . import __version__
from .config import Config
from .db import init_db, make_engine, make_session_factory, session_scope
from .diff import diff_snapshots
from .doctor import health_summary
from .export import export_snapshot_json
from .snapshot import create_snapshot, list_snapshots
from .ytmusic_client import AuthFileMissingError, YTMusicClient

# All option/argument metadata is attached via ``Annotated[...]`` rather than
# ``typer.Option(...)`` defaults so that ruff's ``B008`` (function call in
# argument default) stays clean. This is also the modern Typer-recommended
# pattern.

app = typer.Typer(
    help="Sync, backup, and repair your YouTube Music liked songs.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Set up authentication with music providers.", no_args_is_help=True)
scan_app = typer.Typer(help="Scan music providers for liked songs.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(scan_app, name="scan")

console = Console()
err_console = Console(stderr=True)


def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory."""
    cfg = Config.load()
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)


def _fail(msg: str, code: int = 1) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"likesurgeon {__version__}")
        raise typer.Exit()


# ``is_eager=True`` is required: the root ``Typer`` has ``no_args_is_help=True``,
# which short-circuits with the "Missing command" help text *before* a non-eager
# callback body runs. An eager option callback fires before that check.
@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    pass


@app.command()
def init() -> None:
    """Initialize the local app directory and SQLite database."""
    cfg, _ = _bootstrap()
    console.print(f"[green]✓[/green] Initialized at [cyan]{cfg.app_dir}[/cyan]")
    console.print(f"  Database: [cyan]{cfg.db_path}[/cyan]")


@auth_app.command("ytmusic")
def auth_ytmusic() -> None:
    """Print instructions for setting up ytmusicapi browser-header auth.

    OAuth is intentionally not supported in MVP 0.1 — see README roadmap.
    """
    cfg = Config.load()
    cfg.ensure_app_dir()
    target = cfg.ytmusic_browser_path
    console.print("[bold]YouTube Music browser-header setup[/bold]")
    console.print(
        "1. Open YouTube Music in your browser and sign in.\n"
        "2. Open DevTools → Network → find an authenticated POST request to "
        "[cyan]/youtubei/v1/browse[/cyan] and copy its raw request headers.\n"
        "   (See [cyan]https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html[/cyan])\n"
        "3. Run [cyan]uv run ytmusicapi browser[/cyan] and paste the headers when prompted.\n"
        f"4. Move the generated [cyan]browser.json[/cyan] to [cyan]{target}[/cyan]."
    )


@scan_app.command("ytmusic")
def scan_ytmusic(
    limit: Annotated[
        int, typer.Option(help="Maximum number of liked songs to fetch.")
    ] = 5000,
) -> None:
    """Fetch YouTube Music liked songs and store a snapshot."""
    cfg, factory = _bootstrap()
    client = YTMusicClient(browser_path=cfg.ytmusic_browser_path)
    try:
        items = client.fetch_liked_songs(limit=limit)
    except AuthFileMissingError as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "ytmusic", items)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} tracks)."
        )


@app.command()
def snapshots() -> None:
    """List previous snapshots."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        rows = list_snapshots(session)

    table = Table(title="Snapshots")
    table.add_column("ID", justify="right")
    table.add_column("Source")
    table.add_column("Created (UTC)")
    table.add_column("Items", justify="right")
    for s in rows:
        table.add_row(
            str(s.id),
            s.source,
            s.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            str(s.raw_count),
        )
    if not rows:
        console.print(
            "[yellow]No snapshots yet.[/yellow] "
            "Run [cyan]likesurgeon scan ytmusic[/cyan]."
        )
        return
    console.print(table)


@app.command()
def diff(
    old_snapshot_id: Annotated[int, typer.Argument(help="Older snapshot id.")],
    new_snapshot_id: Annotated[int, typer.Argument(help="Newer snapshot id.")],
) -> None:
    """Compare two snapshots and show added/removed tracks."""
    _, factory = _bootstrap()
    try:
        with session_scope(factory) as session:
            result = diff_snapshots(session, old_snapshot_id, new_snapshot_id)
    except ValueError as e:
        _fail(str(e), code=1)

    console.print(
        f"[bold]Common:[/bold] {result.common_count}  "
        f"[bold green]Added:[/bold green] {len(result.added)}  "
        f"[bold red]Removed:[/bold red] {len(result.removed)}"
    )

    if result.added:
        t = Table(title="Added")
        t.add_column("Track ID", justify="right")
        t.add_column("Title")
        t.add_column("Artists")
        t.add_column("Video ID")
        for s in result.added:
            t.add_row(str(s.track_id), s.title, ", ".join(s.artists), s.video_id or "")
        console.print(t)

    if result.removed:
        t = Table(title="Removed")
        t.add_column("Track ID", justify="right")
        t.add_column("Title")
        t.add_column("Artists")
        t.add_column("Video ID")
        for s in result.removed:
            t.add_row(str(s.track_id), s.title, ", ".join(s.artists), s.video_id or "")
        console.print(t)


@app.command()
def export(
    snapshot_id: Annotated[int, typer.Argument(help="Snapshot id to export.")],
    format: Annotated[
        str, typer.Option("--format", "-f", help="Export format. Currently 'json'.")
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write to file instead of stdout."),
    ] = None,
) -> None:
    """Export a snapshot's tracks."""
    fmt = format.lower()
    if fmt != "json":
        _fail(f"Unsupported format: {format!r}. Currently only 'json' is supported.", code=2)

    _, factory = _bootstrap()
    try:
        with session_scope(factory) as session:
            payload = export_snapshot_json(session, snapshot_id)
    except ValueError as e:
        _fail(str(e), code=1)

    if output is not None:
        output.write_text(payload, encoding="utf-8")
        console.print(f"[green]✓[/green] Wrote [cyan]{output}[/cyan]")
    else:
        # Use the bare print so machine consumers can pipe stdout cleanly.
        print(payload)


@app.command()
def doctor() -> None:
    """Print a basic health summary using stored data."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        report = health_summary(session, source="ytmusic")

    console.print(f"[bold]Total snapshots:[/bold] {report.total_snapshots}")
    if report.latest_source is None:
        console.print(
            "[yellow]No snapshots yet.[/yellow] "
            "Run [cyan]likesurgeon scan ytmusic[/cyan] to capture your liked songs."
        )
        return
    console.print(f"[bold]Latest source:[/bold] {report.latest_source}")
    console.print(f"[bold]Latest liked songs count:[/bold] {report.latest_count}")
    if report.last_diff is None:
        console.print("[dim]No previous snapshot to compare against yet.[/dim]")
    else:
        console.print(
            f"[bold]Compared to previous:[/bold] "
            f"+{len(report.last_diff.added)} added, "
            f"-{len(report.last_diff.removed)} removed, "
            f"{report.last_diff.common_count} common."
        )


if __name__ == "__main__":
    app()
```

- [ ] **Step 2: Verify Typer wires everything up**

Run: `uv run likesurgeon --help`
Expected: top-level usage block listing `init`, `auth`, `scan`, `snapshots`, `diff`, `export`, `doctor`.

- [ ] **Step 3: Verify subcommand discovery**

Run: `uv run likesurgeon auth --help && uv run likesurgeon scan --help`
Expected: each lists `ytmusic` as a subcommand.

- [ ] **Step 4: End-to-end smoke (no real auth required)**

Run:
```bash
LIKE_SURGEON_HOME=/tmp/ls-smoke-$$ uv run likesurgeon init
LIKE_SURGEON_HOME=/tmp/ls-smoke-$$ uv run likesurgeon snapshots
LIKE_SURGEON_HOME=/tmp/ls-smoke-$$ uv run likesurgeon doctor
LIKE_SURGEON_HOME=/tmp/ls-smoke-$$ uv run likesurgeon auth ytmusic
LIKE_SURGEON_HOME=/tmp/ls-smoke-$$ uv run likesurgeon scan ytmusic; echo exit=$?
```
Expected:
- `init` prints the resolved app dir + db path.
- `snapshots` prints the "No snapshots yet" message.
- `doctor` prints "Total snapshots: 0" and the prompt to run scan.
- `auth ytmusic` prints Rich-formatted browser-setup instructions.
- Final `scan ytmusic` prints the missing-auth error and exits with `exit=2`.

- [ ] **Step 5: Lint everything**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!`

- [ ] **Step 6: Run the full test suite once more**

Run: `uv run pytest`
Expected: 24 passed.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/cli.py
git commit -m "feat(cli): typer commands for init, auth, scan, snapshots, diff, export, doctor"
```

---

## Task 11: README

**Files:**
- Modify: `README.md` (overwrite the stub created in Task 1)

- [ ] **Step 1: Overwrite `README.md`**

```markdown
# like-surgeon

> Sync, backup, and repair your YouTube Music liked songs.

**Status:** Early MVP 0.1 — read-only scanner. Local-first. No server, no destructive actions.

## What it does today

- Authenticates with YouTube Music via [`ytmusicapi`](https://ytmusicapi.readthedocs.io/).
- Fetches your liked songs and stores them as a snapshot in a local SQLite database.
- Lists historical snapshots, diffs any two of them, and exports a snapshot to JSON.
- Prints a health summary so you can spot disappearing tracks before they're gone for good.

Snapshots preserve **point-in-time metadata**: each scan freezes the title, artists, album, raw payload, etc. that YouTube Music returned at that moment. If a video later gets renamed, re-credited, or has its raw payload reshaped by `ytmusicapi`, the old snapshot still exports the values you originally captured — that's the "backup" half of the project name.

## Roadmap

| Milestone   | Scope                                                                |
|-------------|----------------------------------------------------------------------|
| **0.1**     | Read-only YouTube Music liked-songs scanner + local snapshots (this) |
| 0.2         | YouTube liked *videos* via the official YouTube Data API             |
| 0.3         | Matching engine for missing / "ghost" / pointer-drift tracks         |
| 0.4         | Backup playlist support                                              |
| 1.0         | Local web UI / Electron app                                          |

## Install

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/zeikar/like-surgeon.git
cd like-surgeon
uv sync --extra dev
```

The CLI is exposed as `likesurgeon`. Use it via `uv run likesurgeon ...` or activate the venv (`source .venv/bin/activate`) and call `likesurgeon` directly.

## Usage

### 1. Initialize local storage

```bash
uv run likesurgeon init
```

Creates `~/.like-surgeon/` and the SQLite database at `~/.like-surgeon/like-surgeon.sqlite`. Override the location with `LIKE_SURGEON_HOME=/some/path`.

### 2. Authenticate with YouTube Music

```bash
uv run likesurgeon auth ytmusic
```

This prints the exact `ytmusicapi browser` setup steps and the destination file path. The command does **not** perform the auth flow itself — it tells you which `ytmusicapi` invocation to run and where to save the resulting JSON.

> **MVP 0.1 supports browser-header auth only.** OAuth was deferred because `ytmusicapi` ≥ 1.7 requires a user-supplied Google Cloud client id / secret. OAuth support is on the 0.2 roadmap.

### 3. Scan liked songs into a snapshot

```bash
uv run likesurgeon scan ytmusic
uv run likesurgeon scan ytmusic --limit 1000
```

### 4. List snapshots

```bash
uv run likesurgeon snapshots
```

### 5. Diff two snapshots

```bash
uv run likesurgeon diff <old_snapshot_id> <new_snapshot_id>
```

### 6. Export a snapshot

```bash
uv run likesurgeon export <snapshot_id> --format json
uv run likesurgeon export <snapshot_id> --format json --output likes.json
```

### 7. Doctor (health summary)

```bash
uv run likesurgeon doctor
```

Prints total snapshot count, the latest snapshot's track count, and the diff against the previous snapshot if one exists.

## Caveat: ytmusicapi

YouTube Music has no official public API. `ytmusicapi` is a community-maintained client built on top of YouTube Music's web requests, so behavior can change when YouTube Music updates its frontend. If a scan fails, check the [`ytmusicapi` issue tracker](https://github.com/sigma67/ytmusicapi/issues) before filing a bug here.

## Local layout

```
~/.like-surgeon/
├── like-surgeon.sqlite       # all snapshots, tracks, and metadata
└── browser.json              # ytmusicapi browser-header auth
```

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
```

- [ ] **Step 2: End turn — commit on next turn**

```bash
git add README.md
git commit -m "docs: project README with status, usage, and roadmap"
```

---

## Task 12: Final verification

- [ ] **Step 1: Run all checks together**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```
Expected:
- `ruff check`: `All checks passed!`
- `ruff format --check`: nothing to reformat (or, if it suggests reformatting, run `uv run ruff format .`, commit, and re-run — note that `format` may change minor whitespace; if it does, treat as a separate cleanup commit).
- `pytest -v`: 24 passed (11 normalize + 1 snapshot + 5 diff + 3 export + 4 ytmusic_client).

- [ ] **Step 2: Smoke-test the installed entrypoint**

Run: `uv run likesurgeon --version`
Expected: `likesurgeon 0.1.0`.

- [ ] **Step 3: Confirm tree matches plan**

Run: `git ls-files`
Expected: includes everything listed in "Repository layout" near the top of this plan.

- [ ] **Step 4: (If formatter ran) commit any whitespace fixes — on next turn**

```bash
git status
# if anything was reformatted in Step 1
git add -u
git commit -m "style: ruff format pass"
```

---

## Self-review notes (plan-only — not part of the repo)

> These notes live with the plan in `docs/superpowers/plans/`. They're for the
> author/reviewer to track spec coverage and intentional deviations. Nothing
> here is consumed by tooling, and nothing here ships in the project's user-
> facing surface, so there's no cleanup task at the end — just leave them.

**Spec coverage check:**
- [x] `likesurgeon init` — Task 10.
- [x] `likesurgeon auth ytmusic` — Task 10 (browser-header method only; prints instructions). OAuth deferred to 0.2 — see "Auth scope for MVP 0.1" header.
- [x] `likesurgeon scan ytmusic` — Task 10 (uses Task 9 client, Task 6 ingest).
- [x] `likesurgeon snapshots` — Task 10 (Rich table).
- [x] `likesurgeon diff` — Task 10 + Task 7.
- [x] `likesurgeon export ... --format json` — Task 10 + Task 8.
- [x] `likesurgeon doctor` — Task 10 + Task 8.
- [x] Track / Snapshot / SnapshotItem schema with all listed fields — Task 4 (note: `dedupe_key` added beyond the spec to enforce identity at the DB level — documented in `models.py`).
- [x] Identity = `source + video_id` first, canonical_key fallback — Task 6 (`_ytmusic_to_record`).
- [x] Canonical key normalization with all listed noise words — Task 3.
- [x] Config dir, env override, default paths — Task 2.
- [x] Rich tables, clear errors, helpful auth-missing message — Task 10.
- [x] Tests for normalization and diff — Tasks 3 and 7.
- [x] Snapshot ingestion regression test (duplicate identities allowed) — Task 6 (`tests/test_snapshot.py`). Pins the `(snapshot_id, position)`-only uniqueness policy.
- [x] Tests for YTMusicClient (success + missing-auth paths) — Task 9.
- [x] `ruff check`, `ruff format`, `pytest` pass — Task 12.
- [x] Read-only except local DB writes; no destructive actions — verified across all tasks.
- [x] No hardcoded secrets — verified.
- [x] README per spec — Task 11.

**Beyond spec (intentional):**
- Added `dedupe_key` column for clean DB-level uniqueness without partial indexes.
- Split `dtos.py` from `models.py` to keep ORM and Pydantic concerns separate.
- **`SnapshotItem` carries point-in-time metadata** (`title`, `artists`, `album`, `duration_seconds`, `thumbnails_json`, `canonical_key`, `video_id`, `raw_json`). The spec lists only `snapshot_id / track_id / position` for `SnapshotItem`, but a "snapshot" without preserved metadata isn't a backup — re-liking the same video under a new title would silently rewrite the old snapshot's exported title. `Track` remains the master row for *latest* known metadata; `SnapshotItem` is the frozen capture. `diff` and `export` both read `SnapshotItem`. Identity for diffing is still `Track.id` (so renames don't fork into added/removed). Regression tested in `tests/test_diff.py::test_diff_uses_point_in_time_metadata` and `tests/test_export.py::test_export_preserves_point_in_time_metadata`.

**Deliberately deferred (per spec "do not overbuild"):**
- No GUI / web UI / Electron.
- No repair actions.
- No matching engine (RapidFuzz is declared as a dependency for MVP 0.3, unused now).
- No YouTube Data API integration (MVP 0.2).
- No CSV / M3U export (`export_snapshot_*` functions can be added without changing the CLI signature).
- No OAuth for ytmusicapi (browser-header only) — see "Auth scope for MVP 0.1" header. ytmusicapi ≥ 1.7 OAuth requires a Google Cloud client id / secret + an `OAuthCredentials` instance, which adds onboarding friction we don't want in 0.1.
