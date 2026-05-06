# like-surgeon MVP 0.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add YouTube Data API support, music-candidate classification, and a cross-source comparison + diagnosis pipeline so users can see which YouTube likes are music, which are missing from YouTube Music, and which look like pointer drift — all read-only.

**Architecture:** New `youtube_client.py` wraps Google's OAuth/Data-API client (parallel to `ytmusic_client.py`). A pure `classify.py` heuristic decides if a YouTube video is "music-like." `snapshot.py` is refactored from a hardcoded ytmusic translator to a `_TRANSLATORS` dispatch map keyed by source. New `compare.py` module diffs the latest snapshot of each source via three-stage matching (video_id → canonical_key → RapidFuzz). New `diagnosis` / `diagnosis_items` tables persist `compare-likes` runs; new `issues` CLI surfaces them. `doctor` is refactored to aggregate across sources and produce a simple match-rate health score.

**Tech Stack:** Python ≥3.11 · uv · Typer · Rich · Pydantic v2 · SQLAlchemy 2 · ytmusicapi · **google-auth-oauthlib · google-api-python-client (new)** · **RapidFuzz (now used)** · pytest · ruff

---

## Repository layout produced by this plan

```
src/likesurgeon/
├── __init__.py
├── cli.py                    # MODIFY: + auth youtube, scan youtube-likes, compare-likes, issues; doctor refactor
├── classify.py               # NEW
├── compare.py                # NEW
├── config.py                 # MODIFY: + youtube-oauth-client.json + youtube-token.json paths
├── db.py                     # MODIFY: PRAGMA foreign_keys=ON (no in-place migration; 0.1 DBs reset)
├── diagnosis.py              # NEW
├── diff.py
├── doctor.py                 # MODIFY: multi-source + health score
├── dtos.py
├── export.py
├── models.py                 # MODIFY: tz-aware DateTime; SnapshotItem classifier cols; Diagnosis tables
├── normalize.py
├── snapshot.py               # MODIFY: _TRANSLATORS dispatch; source rename; classifier integration
├── youtube_client.py         # NEW
└── ytmusic_client.py
tests/
├── __init__.py
├── conftest.py               # MODIFY: any source-name renames in fixture-style helpers (none currently)
├── test_classify.py          # NEW
├── test_compare.py           # NEW
├── test_diagnosis.py         # NEW
├── test_diff.py
├── test_doctor.py            # NEW
├── test_export.py
├── test_normalize.py
├── test_snapshot.py
├── test_youtube_client.py    # NEW
└── test_ytmusic_client.py
```

## Commit policy

Same as 0.1: each task is one turn of work + verification, then end the turn. On the *next* turn, run the suggested `git add` / `git commit`. Subagent flow collapses this — implementer commits at the end, controller reviews after.

## Source naming

We change the persisted source constant from `"ytmusic"` to `"ytmusic_liked_songs"` in Task 1. The new YouTube source is `"youtube_liked_videos"`. CLI subcommands stay short (`scan ytmusic`, `scan youtube-likes`); only the on-disk `Snapshot.source` / `Track.source` values and the in-code constants change.

**Existing persisted rows are NOT migrated** — this is a code-only rename. Per the migration philosophy above, 0.1 DBs must be deleted and rescanned (see README "Upgrading from 0.1"). Any 0.1 row left as `source='ytmusic'` would simply be invisible to 0.2 lookups; we keep the breaking change visible rather than papering over it with a one-shot UPDATE.

## Schema migration philosophy

0.2 ships a breaking schema change vs. 0.1 (source-string rename + three new `snapshot_items` columns + two new tables). At MVP scope with effectively zero installed base, in-place migration is **not worth its weight** — the cost of a robust column/data migration exceeds the cost of "delete the SQLite file and rescan." `Base.metadata.create_all(engine)` handles fresh DBs, and the README documents the upgrade step.

Proper migrations (Alembic, etc.) land in 0.3+ once there's a real user base to protect.

---

## Task 1: 0.1 leftover cleanup + source rename

Bundle the four Minor follow-ups flagged at the end of 0.1 plus the `"ytmusic"` → `"ytmusic_liked_songs"` rename. Done first because every later task touches at least one of the affected files.

**Files:**
- Modify: `src/likesurgeon/db.py`
- Modify: `src/likesurgeon/models.py`
- Modify: `src/likesurgeon/cli.py`
- Modify: `src/likesurgeon/doctor.py`
- Modify: `src/likesurgeon/snapshot.py`
- Modify: `tests/test_diff.py`
- Modify: `tests/test_export.py`
- Modify: `tests/test_snapshot.py`

- [ ] **Step 1: Enable SQLite FK pragma**

In `src/likesurgeon/db.py`, replace the file with:

```python
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

    0.2 ships with a breaking schema change vs. 0.1 (source-string rename
    plus three new SnapshotItem columns added in Task 3). Existing 0.1 DBs
    must be deleted and rescanned — a deliberate MVP choice; see README's
    "Upgrading from 0.1" section. Alembic / proper migrations land in 0.3+
    once there's a real user base to protect.
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
```

- [ ] **Step 2: Make all `DateTime` columns tz-aware**

In `src/likesurgeon/models.py`, change every `mapped_column(DateTime, ...)` to `mapped_column(DateTime(timezone=True), ...)`.

There are three such columns:
- `Track.first_seen_at`
- `Track.last_seen_at`
- `Snapshot.created_at`

After the change those lines should read:

```python
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

```python
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

- [ ] **Step 3: Annotate `_fail` as `NoReturn`**

In `src/likesurgeon/cli.py`:

Add to the imports near the top of the file:

```python
from typing import Annotated, NoReturn
```

Change the `_fail` signature from:

```python
def _fail(msg: str, code: int = 1) -> None:
```

to:

```python
def _fail(msg: str, code: int = 1) -> NoReturn:
```

(No body changes; the function already always raises.)

- [ ] **Step 4: Rename source `"ytmusic"` → `"ytmusic_liked_songs"` everywhere**

The string appears in code and tests. Use ripgrep to find every occurrence before editing:

```bash
cd /Users/zeikar/Developer/Projects/like-surgeon
rg -F '"ytmusic"' src tests
rg -F "'ytmusic'" src tests
```

You should find these (plus the `auth ytmusic` / `scan ytmusic` CLI command names — DO NOT touch those, only the source-string occurrences):

- `src/likesurgeon/cli.py` — inside `scan_ytmusic`: `create_snapshot(session, "ytmusic", items)` → `create_snapshot(session, "ytmusic_liked_songs", items)`
- `src/likesurgeon/cli.py` — inside `doctor`: `health_summary(session, source="ytmusic")` → `health_summary(session, source="ytmusic_liked_songs")`
- `src/likesurgeon/doctor.py` — `def health_summary(session: Session, source: str = "ytmusic")` → `def health_summary(session: Session, source: str = "ytmusic_liked_songs")`
- `tests/test_diff.py` — five places where `"ytmusic"` appears in `create_snapshot(session, "ytmusic", ...)` → `"ytmusic_liked_songs"`
- `tests/test_export.py` — three places, same shape
- `tests/test_snapshot.py` — one place, same shape

Do NOT change:
- `auth_ytmusic` function name or `auth ytmusic` / `scan ytmusic` Typer command names — these stay short; the CLI command is `scan ytmusic`, the persisted source is `"ytmusic_liked_songs"`.
- The CLI's user-facing strings that say "YouTube Music" prose-style — those are already prose.

- [ ] **Step 5: Make `doctor` source-aware**

In `src/likesurgeon/doctor.py`, replace the file with:

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


def health_summary(
    session: Session, source: str = "ytmusic_liked_songs"
) -> HealthReport:
    """Summary scoped to ``source``: total count, latest, prev-diff."""
    total = (
        session.scalar(
            select(func.count(Snapshot.id)).where(Snapshot.source == source)
        )
        or 0
    )
    latest = latest_snapshot(session, source=source)
    if latest is None:
        return HealthReport(
            total_snapshots=total,
            latest_source=None,
            latest_count=None,
            last_diff=None,
        )

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

The change: `total` now filters by source, matching the rest of the report. The MultiSourceHealth report comes in Task 11; for now this just makes the single-source report internally consistent.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: 26 passed (no test count change yet; only string renames + types).

- [ ] **Step 7: Lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: All checks passed.

- [ ] **Step 8: Smoke FK pragma is actually on**

Run:
```bash
uv run python -c "
from sqlalchemy import create_engine, text
from likesurgeon.db import init_db
e = create_engine('sqlite:///:memory:', future=True)
init_db(e)
with e.connect() as c:
    print('foreign_keys =', c.execute(text('PRAGMA foreign_keys')).scalar())
"
```
Expected: `foreign_keys = 1`

- [ ] **Step 9: End turn — commit on next turn**

```bash
git add src/likesurgeon/db.py src/likesurgeon/models.py src/likesurgeon/cli.py src/likesurgeon/doctor.py src/likesurgeon/snapshot.py tests/test_diff.py tests/test_export.py tests/test_snapshot.py
git commit -m "chore: 0.1 leftovers + ytmusic source rename for 0.2

- Enable SQLite PRAGMA foreign_keys=ON via SQLAlchemy event listener
- All DateTime columns now timezone=True (YouTube published_at needs tz)
- _fail annotated NoReturn so type checkers see the unreachable branches
- Rename source string ytmusic -> ytmusic_liked_songs (consistent with
  the new youtube_liked_videos that 0.2 introduces)
- doctor.total_snapshots is now source-scoped, matching latest_count

NOTE: 0.2 is a breaking schema change vs 0.1. Existing local DBs must be
deleted and rescanned; in-place migration is deferred to 0.3+ once
there's a real user base to protect. README documents this in the
'Upgrading from 0.1' section."
```

---

## Task 2: Music-candidate classifier (TDD)

Pure-functional heuristic. No I/O, no DB. Deterministic given inputs. Used by Task 5's translator and (later) by `compare-likes` to filter "what counts as music" in a YouTube liked-videos snapshot.

**Files:**
- Create: `tests/test_classify.py`
- Create: `src/likesurgeon/classify.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_classify.py
"""Heuristic music-candidate classifier tests."""

from __future__ import annotations

from likesurgeon.classify import Classification, classify


def _c(
    title: str = "",
    channel: str | None = None,
    description: str | None = None,
) -> Classification:
    return classify(title=title, channel=channel, description=description)


def test_official_music_video_is_music():
    res = _c(title="Song Name (Official Music Video)", channel="ArtistVEVO")
    assert res.is_music_candidate is True
    assert res.score >= 2
    assert "official music video" in res.reason.lower()


def test_topic_channel_is_music():
    res = _c(title="Song Name", channel="Artist - Topic")
    assert res.is_music_candidate is True
    assert "topic" in res.reason.lower()


def test_provided_to_youtube_in_description_is_music():
    res = _c(
        title="Song Name",
        channel="Some Channel",
        description="Provided to YouTube by SomeLabel\n\nSong · Artist · Album",
    )
    assert res.is_music_candidate is True


def test_artist_dash_title_pattern_is_music():
    res = _c(title="The Beatles - Hey Jude", channel="Some Channel")
    assert res.is_music_candidate is True
    assert "artist - title" in res.reason.lower()


def test_lyrics_video_is_music():
    res = _c(title="Song Name (Lyrics)", channel="LyricsChannel")
    assert res.is_music_candidate is True


def test_visualizer_is_music():
    res = _c(title="Song Name | Visualizer", channel="Whoever")
    assert res.is_music_candidate is True


def test_vlog_title_is_not_music():
    res = _c(title="Daily vlog #42", channel="VlogChannel")
    assert res.is_music_candidate is False
    assert "vlog" in res.reason.lower()


def test_tutorial_is_not_music():
    res = _c(title="How to bake bread - Tutorial", channel="CookChannel")
    assert res.is_music_candidate is False


def test_gameplay_is_not_music():
    res = _c(title="Elden Ring boss gameplay walkthrough", channel="GamerName")
    assert res.is_music_candidate is False


def test_negative_outweighs_weak_positive():
    """A title with both 'audio' (weak +) and 'reaction' (strong -) is NOT music."""
    res = _c(title="My reaction to the new audio drama", channel="ReactionChannel")
    assert res.is_music_candidate is False


def test_plain_unrelated_title_is_not_music():
    res = _c(title="Why I moved out of NYC", channel="LifestyleChannel")
    assert res.is_music_candidate is False
    assert res.score == 0


def test_classification_is_pure():
    """Same input → same output, no time/global state."""
    a = _c(title="Song (Official MV)", channel="ArtistVEVO")
    b = _c(title="Song (Official MV)", channel="ArtistVEVO")
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_classify.py -v`
Expected: all tests fail with `ModuleNotFoundError: No module named 'likesurgeon.classify'`.

- [ ] **Step 3: Implement `src/likesurgeon/classify.py`**

```python
"""Heuristic music-candidate classifier for YouTube liked videos.

Pure-functional. No I/O. Inputs are the fields surfaced by ``playlistItems``
(title, channel name, description). Output is a small frozen dataclass so
callers can record ``score`` and ``reason`` alongside the boolean verdict
in ``SnapshotItem``.

The classifier is intentionally conservative: positive signals add points,
negative signals subtract. ``is_music_candidate`` flips True when the net
score reaches the positive threshold AND there's at least one positive
signal. This keeps "How I built a synth - vlog" out of the music bucket
even though "synth" is musical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Each entry is (regex, weight, label). Regexes are compiled IGNORECASE.
_POSITIVE_SIGNALS: tuple[tuple[str, int, str], ...] = (
    (r"\bofficial\s+music\s+video\b", 3, "official music video"),
    (r"\bofficial\s+mv\b", 3, "official mv"),
    (r"\bofficial\s+audio\b", 3, "official audio"),
    (r"\bofficial\s+video\b", 2, "official video"),
    (r"\blyric\s+video\b", 2, "lyric video"),
    (r"\blyrics\b", 2, "lyrics"),
    (r"\bvisualizer\b", 2, "visualizer"),
    (r"\bm/v\b", 2, "m/v"),
    (r"\bmv\b", 1, "mv"),
    (r"\baudio\b", 1, "audio"),
)

# A "channel name ends with - Topic" is a strong signal: YouTube's autogenerated
# music channels are named "<Artist> - Topic".
_CHANNEL_TOPIC_RE = re.compile(r"\s-\s*topic\s*$", re.IGNORECASE)

# "Artist - Title" pattern in title (with at least one space on each side of the dash).
# Conservative: requires the dash to be surrounded by spaces, not embedded in a word.
_ARTIST_DASH_TITLE_RE = re.compile(r"^[^-\n]{2,}\s+-\s+[^-\n]{2,}$")

# Description boilerplate that YouTube's auto-generated music videos always include.
_DESCRIPTION_PROVIDED_RE = re.compile(
    r"provided to youtube by", re.IGNORECASE
)

# Strong negative signals that override most positive signals.
_NEGATIVE_SIGNALS: tuple[tuple[str, int, str], ...] = (
    (r"\bvlog\b", 3, "vlog"),
    (r"\btutorial\b", 3, "tutorial"),
    (r"\bhow\s+to\b", 2, "how-to"),
    (r"\breview\b", 2, "review"),
    (r"\breaction\b", 3, "reaction"),
    (r"\bgameplay\b", 3, "gameplay"),
    (r"\bwalkthrough\b", 2, "walkthrough"),
    (r"\bnews\b", 2, "news"),
    (r"\bshorts?\b", 2, "shorts"),
    (r"\bunboxing\b", 2, "unboxing"),
    (r"\binterview\b", 2, "interview"),
)

# Net score must reach this to flip the boolean True.
_POSITIVE_THRESHOLD = 2


@dataclass(frozen=True)
class Classification:
    is_music_candidate: bool
    score: int
    reason: str


def _scan(text: str, signals: tuple[tuple[str, int, str], ...]) -> tuple[int, list[str]]:
    """Return (total_weight, matched_labels) for ``signals`` against ``text``."""
    total = 0
    labels: list[str] = []
    for pattern, weight, label in signals:
        if re.search(pattern, text, flags=re.IGNORECASE):
            total += weight
            labels.append(label)
    return total, labels


def classify(
    title: str,
    channel: str | None = None,
    description: str | None = None,
) -> Classification:
    """Score a YouTube video as a music candidate or not."""
    title = title or ""
    channel = channel or ""
    description = description or ""

    pos_score, pos_labels = _scan(title, _POSITIVE_SIGNALS)
    neg_score, neg_labels = _scan(title, _NEGATIVE_SIGNALS)
    # Negative signals also scan the channel name and description body.
    extra_neg, extra_neg_labels = _scan(channel + " " + description, _NEGATIVE_SIGNALS)
    neg_score += extra_neg
    neg_labels.extend(extra_neg_labels)

    extra_labels: list[str] = []
    if _CHANNEL_TOPIC_RE.search(channel):
        pos_score += 3
        extra_labels.append("topic channel")
    if _ARTIST_DASH_TITLE_RE.search(title):
        # +2 (one notch above threshold) so an "Artist - Title" titled video
        # on its own clears the music bar even when no other signal fires.
        # This is intentionally stronger than the +1 from a stray "audio"
        # or "mv" token because the dash-pattern is what actual music-channel
        # uploads look like ("The Beatles - Hey Jude").
        pos_score += 2
        extra_labels.append("artist - title")
    if _DESCRIPTION_PROVIDED_RE.search(description):
        pos_score += 3
        extra_labels.append("provided to youtube by")

    score = pos_score - neg_score
    reasons: list[str] = []
    if pos_labels or extra_labels:
        reasons.append("positive: " + ", ".join(pos_labels + extra_labels))
    if neg_labels:
        reasons.append("negative: " + ", ".join(neg_labels))

    is_music = (
        score >= _POSITIVE_THRESHOLD
        and bool(pos_labels or extra_labels)
        and not (neg_score >= 3)
    )
    reason = " | ".join(reasons) if reasons else "no signals"
    return Classification(is_music_candidate=is_music, score=score, reason=reason)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: 12 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: 38 passed (26 from 0.1 + 12 new).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/classify.py tests/test_classify.py`
Expected: All checks passed.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/classify.py tests/test_classify.py
git commit -m "feat(classify): heuristic music-candidate classifier with TDD tests"
```

---

## Task 3: Schema additions

Adds three nullable columns to `snapshot_items` (classifier output) and two new tables (`diagnoses`, `diagnosis_items`). `Base.metadata.create_all` handles the new tables; the new columns ride along on a fresh DB. Existing 0.1 DBs are out of scope per the breaking-change policy in Task 1's commit message.

**Files:**
- Modify: `src/likesurgeon/models.py`

- [ ] **Step 1: Add classifier columns + Diagnosis tables to `models.py`**

In `src/likesurgeon/models.py`, do two things:

(a) Add three nullable columns to `SnapshotItem` after `raw_json`:

```python
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Music-candidate classifier output (Task 2). Populated by sources whose
    # items aren't guaranteed-music (i.e. youtube_liked_videos). NULL for
    # ytmusic_liked_songs since those are always music by definition.
    is_music_candidate: Mapped[bool | None] = mapped_column(nullable=True)
    music_candidate_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    music_candidate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(b) Append two new ORM models at the bottom of the file (after `SnapshotItem`):

```python
class Diagnosis(Base):
    """A persisted ``compare-likes`` run."""

    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ytmusic_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )
    youtube_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True
    )

    items: Mapped[list[DiagnosisItem]] = relationship(
        back_populates="diagnosis", cascade="all, delete-orphan"
    )


class DiagnosisItem(Base):
    """A single finding produced by a ``compare-likes`` run.

    ``issue_type`` is one of:
      - ``possibly_missing_from_ytmusic`` — music candidate liked on YouTube
        but with no corresponding YT Music track (the high-priority bucket).
      - ``possible_pointer_drift`` — fuzzy match between sources, neither
        ``video_id`` nor ``canonical_key`` exact.
      - ``ytmusic_only`` — YT Music has it but no YT like (informational —
        often just "user never liked it on YouTube").
    """

    __tablename__ = "diagnosis_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), index=True
    )
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column()  # SQLAlchemy maps Python float -> SQLite REAL
    reason: Mapped[str] = mapped_column(Text)
    source_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    related_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="open")

    diagnosis: Mapped[Diagnosis] = relationship(back_populates="items")
```

> Note: `confidence` is a plain `float`-typed column. SQLAlchemy maps Python `float` to SQLite `REAL` automatically without any extra import.

(Task 1 already enabled the FK pragma and kept `init_db` as a thin wrapper around `Base.metadata.create_all`. Nothing further to do in `db.py` for Task 3 — the new tables come for free.)

- [ ] **Step 2: Smoke-test schema creation on a fresh DB**

Run:
```bash
uv run python -c "
from sqlalchemy import create_engine, inspect
from likesurgeon.db import init_db
e = create_engine('sqlite:///:memory:', future=True)
init_db(e)
ins = inspect(e)
print(sorted(ins.get_table_names()))
print('snapshot_items cols:', sorted(c['name'] for c in ins.get_columns('snapshot_items')))
print('diagnoses cols:', sorted(c['name'] for c in ins.get_columns('diagnoses')))
print('diagnosis_items cols:', sorted(c['name'] for c in ins.get_columns('diagnosis_items')))
"
```

Expected:
```
['diagnoses', 'diagnosis_items', 'snapshot_items', 'snapshots', 'tracks']
snapshot_items cols: ['album', 'artists', 'canonical_key', 'duration_seconds', 'id', 'is_music_candidate', 'music_candidate_reason', 'music_candidate_score', 'position', 'raw_json', 'snapshot_id', 'thumbnails_json', 'title', 'track_id', 'video_id']
diagnoses cols: ['created_at', 'id', 'youtube_snapshot_id', 'ytmusic_snapshot_id']
diagnosis_items cols: ['confidence', 'diagnosis_id', 'id', 'issue_type', 'reason', 'related_track_id', 'source_track_id', 'status']
```

- [ ] **Step 3: Run the full suite — confirm no regression**

Run: `uv run pytest`
Expected: 38 passed (26 from Task 1 + 12 classify).

- [ ] **Step 4: Lint**

Run: `uv run ruff check src/likesurgeon/models.py`
Expected: All checks passed.

- [ ] **Step 5: End turn — commit on next turn**

```bash
git add src/likesurgeon/models.py
git commit -m "feat(db): classifier columns on SnapshotItem; Diagnosis tables

- SnapshotItem gains nullable is_music_candidate / music_candidate_score
  / music_candidate_reason columns (filled by classifier on ingest).
- New Diagnosis + DiagnosisItem tables persist compare-likes runs.
- Existing 0.1 DBs need to be deleted and rescanned (breaking change
  policy from Task 1); create_all handles fresh DBs."
```

---

## Task 4: YouTube OAuth client + tests

Wraps `google-auth-oauthlib` and `google-api-python-client` so the rest of the codebase sees one clean entry point. Mirrors the shape of `ytmusic_client.py`: a thin class with a `_build` seam tests can override.

**Files:**
- Modify: `pyproject.toml` (add deps)
- Modify: `src/likesurgeon/config.py` (add youtube paths)
- Create: `src/likesurgeon/youtube_client.py`
- Create: `tests/test_youtube_client.py`

- [ ] **Step 1: Add deps to `pyproject.toml`**

In `pyproject.toml`, change the `dependencies = [...]` block to:

```toml
dependencies = [
  "ytmusicapi>=1.7",
  "typer>=0.12",
  "rich>=13.7",
  "pydantic>=2.7",
  "rapidfuzz>=3.6",
  "sqlalchemy>=2.0",
  "google-auth-oauthlib>=1.2",
  "google-api-python-client>=2.120",
]
```

Run: `uv sync --extra dev`
Expected: installs the two new packages plus their transitive deps; updates `uv.lock`.

- [ ] **Step 2: Add youtube paths to `Config`**

In `src/likesurgeon/config.py`, replace the file with:

```python
"""Local app configuration: paths, env overrides, on-disk layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_DIR_NAME = ".like-surgeon"
DEFAULT_DB_FILENAME = "like-surgeon.sqlite"
YTMUSIC_BROWSER_FILENAME = "browser.json"
YOUTUBE_OAUTH_CLIENT_FILENAME = "youtube-oauth-client.json"
YOUTUBE_TOKEN_FILENAME = "youtube-token.json"
ENV_HOME = "LIKE_SURGEON_HOME"


@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = (
            Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        )
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
            youtube_oauth_client_path=app_dir / YOUTUBE_OAUTH_CLIENT_FILENAME,
            youtube_token_path=app_dir / YOUTUBE_TOKEN_FILENAME,
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 3: Write `src/likesurgeon/youtube_client.py`**

```python
"""Thin wrapper around google-api-python-client for YouTube Data API v3.

The wrapper exists so that:
  * tests can subclass and override ``_service`` / ``_authorize`` to inject a
    fake API client.
  * the CLI gets two clear exceptions (client-secrets missing vs. token
    missing/invalid) instead of leaking google-auth internals.

OAuth flow: the user creates an OAuth 2.0 client of type "Desktop app" in
Google Cloud Console and downloads the JSON to
``~/.like-surgeon/youtube-oauth-client.json``. Calling ``authorize()`` opens
a browser, completes the consent screen, and persists the resulting refresh
token to ``~/.like-surgeon/youtube-token.json``. Subsequent calls reuse and
silently refresh that token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

LIKED_VIDEOS_FALLBACK_PLAYLIST_ID = "LL"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


class ClientSecretsMissingError(FileNotFoundError):
    """Raised when the user hasn't provided a client_secrets JSON yet."""


class AuthorizationRequiredError(RuntimeError):
    """Raised when no token exists (or a stale one cannot refresh)."""


class YouTubeClient:
    def __init__(
        self,
        client_secrets_path: Path | None = None,
        token_path: Path | None = None,
    ) -> None:
        self._client_secrets_path = client_secrets_path
        self._token_path = token_path

    def authorize(self) -> None:
        """Run the InstalledApp flow once, persist the resulting token JSON.

        Idempotent: if a usable token already exists, this is a no-op.
        """
        creds = self._load_token()
        if creds is not None and creds.valid:
            return
        if creds is not None and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_token(creds)
            return
        if self._client_secrets_path is None or not self._client_secrets_path.exists():
            raise ClientSecretsMissingError(
                "No YouTube OAuth client_secrets file found. "
                "Run `likesurgeon auth youtube` and follow the printed instructions."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self._client_secrets_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
        self._save_token(creds)

    def _load_token(self) -> Credentials | None:
        if self._token_path is None or not self._token_path.exists():
            return None
        return Credentials.from_authorized_user_file(str(self._token_path), SCOPES)

    def _save_token(self, creds: Credentials) -> None:
        if self._token_path is None:
            return
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(creds.to_json(), encoding="utf-8")

    def _service(self) -> Any:
        """Return an authorized Data API resource. Raises if not authorized."""
        creds = self._load_token()
        if creds is None:
            raise AuthorizationRequiredError(
                "Not authorized with YouTube. Run `likesurgeon auth youtube` "
                "first — that command walks you through creating the OAuth "
                "client_secrets file (if missing) and runs the consent flow."
            )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._save_token(creds)
            else:
                raise AuthorizationRequiredError(
                    "YouTube token is no longer valid. "
                    "Re-run `likesurgeon auth youtube` to re-consent."
                )
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _resolve_likes_playlist_id(self, service: Any) -> str:
        """Resolve the authenticated user's "liked videos" playlist id.

        Per the YouTube Data API docs, ``channels.list(part='contentDetails',
        mine=True)`` returns the channel's ``relatedPlaylists.likes`` id.
        Using this rather than the magic string ``"LL"`` is more robust if
        Google ever rebrands the well-known fallback. The extra call costs
        1 quota unit (negligible vs. the 100 a full like-list scan uses).

        Falls back to ``"LL"`` if the channel resource doesn't expose the
        id (e.g. brand-account edge cases).
        """
        resp = service.channels().list(part="contentDetails", mine=True).execute()
        for item in resp.get("items") or []:
            likes = (
                (item.get("contentDetails") or {})
                .get("relatedPlaylists", {})
                .get("likes")
            )
            if likes:
                return likes
        return LIKED_VIDEOS_FALLBACK_PLAYLIST_ID

    def fetch_liked_videos(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch up to ``limit`` items from the user's Liked videos playlist.

        Resolves the playlist id from ``channels.relatedPlaylists.likes``
        first, falling back to ``"LL"`` only if the channel resource lacks
        it. Paginates ``playlistItems.list``. Returns the raw item dicts
        as returned by the API; the snapshot translator handles the rest.
        """
        service = self._service()
        playlist_id = self._resolve_likes_playlist_id(service)
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(items) < limit:
            page_size = min(50, limit - len(items))
            request = service.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=page_size,
                pageToken=page_token,
            )
            response = request.execute()
            page_items = response.get("items") or []
            items.extend(page_items)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items[:limit]
```

- [ ] **Step 4: Write `tests/test_youtube_client.py`**

```python
"""Tests for YouTubeClient — fake the API resource via subclass override."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from likesurgeon.youtube_client import (
    AuthorizationRequiredError,
    ClientSecretsMissingError,
    YouTubeClient,
)


class _FakeRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class _FakePlaylistItems:
    """Mimics ``service.playlistItems().list(...).execute()``.

    ``pages`` returned in order; each call records its kwargs in ``calls``.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        if not self._pages:
            return _FakeRequest({"items": []})
        return _FakeRequest(self._pages.pop(0))


class _FakeChannels:
    """Mimics ``service.channels().list(...).execute()``."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.calls.append(kwargs)
        return _FakeRequest(self._response)


class _FakeService:
    def __init__(
        self,
        playlist_items: _FakePlaylistItems,
        channels: _FakeChannels,
    ) -> None:
        self._pi = playlist_items
        self._channels = channels

    def playlistItems(self) -> _FakePlaylistItems:  # noqa: N802 (mimics google API)
        return self._pi

    def channels(self) -> _FakeChannels:
        return self._channels


class _FakeClient(YouTubeClient):
    def __init__(
        self,
        pages: list[dict[str, Any]],
        *,
        likes_playlist_id: str = "LL_resolved",
    ) -> None:
        super().__init__(client_secrets_path=None, token_path=None)
        self._fake_pi = _FakePlaylistItems(pages)
        self._fake_channels = _FakeChannels(
            {
                "items": [
                    {
                        "contentDetails": {
                            "relatedPlaylists": {"likes": likes_playlist_id}
                        }
                    }
                ]
            }
        )

    def _service(self) -> Any:  # type: ignore[override]
        return _FakeService(self._fake_pi, self._fake_channels)


def test_fetch_liked_videos_returns_items_from_single_page():
    pages = [{"items": [{"snippet": {"title": "A", "resourceId": {"videoId": "v1"}}}]}]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=10)
    assert len(result) == 1
    assert result[0]["snippet"]["title"] == "A"


def test_fetch_liked_videos_paginates():
    pages = [
        {
            "items": [{"snippet": {"title": f"T{i}"}} for i in range(50)],
            "nextPageToken": "page2",
        },
        {"items": [{"snippet": {"title": f"T{i}"}} for i in range(50, 75)]},
    ]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=200)
    assert len(result) == 75
    # The second call should have passed pageToken="page2".
    assert c._fake_pi.calls[1]["pageToken"] == "page2"


def test_fetch_liked_videos_uses_resolved_playlist_id():
    """playlistItems.list should target the id resolved from
    channels.contentDetails.relatedPlaylists.likes, not the magic 'LL'."""
    pages = [{"items": [{"snippet": {"title": "X"}}]}]
    c = _FakeClient(pages, likes_playlist_id="LL_real_id")
    c.fetch_liked_videos(limit=10)
    assert c._fake_pi.calls[0]["playlistId"] == "LL_real_id"
    # And the channels resolver was actually consulted.
    assert c._fake_channels.calls[0]["mine"] is True
    assert c._fake_channels.calls[0]["part"] == "contentDetails"


def test_fetch_liked_videos_respects_limit():
    pages = [{"items": [{"snippet": {"title": f"T{i}"}} for i in range(50)]}]
    c = _FakeClient(pages)
    result = c.fetch_liked_videos(limit=10)
    assert len(result) == 10


def test_fetch_liked_videos_handles_empty_page():
    pages = [{}]
    c = _FakeClient(pages)
    assert c.fetch_liked_videos() == []


def test_authorize_without_client_secrets_raises():
    c = YouTubeClient(
        client_secrets_path=Path("/nope/oauth.json"),
        token_path=Path("/nope/token.json"),
    )
    with pytest.raises(ClientSecretsMissingError):
        c.authorize()


def test_service_without_token_raises():
    c = YouTubeClient(
        client_secrets_path=Path("/nope/oauth.json"),
        token_path=Path("/nope/token.json"),
    )
    # _service is what fetch_liked_videos calls internally.
    with pytest.raises(AuthorizationRequiredError):
        c._service()
```

- [ ] **Step 5: Run new tests**

Run: `uv run pytest tests/test_youtube_client.py -v`
Expected: 7 passed.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest`
Expected: 45 passed (38 + 7).

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/likesurgeon/youtube_client.py src/likesurgeon/config.py tests/test_youtube_client.py`
Expected: All checks passed.

- [ ] **Step 8: End turn — commit on next turn**

```bash
git add pyproject.toml uv.lock src/likesurgeon/config.py src/likesurgeon/youtube_client.py tests/test_youtube_client.py
git commit -m "feat(youtube): OAuth-backed YouTube Data API client wrapper with tests

- google-auth-oauthlib + google-api-python-client added as deps.
- YouTubeClient wraps InstalledAppFlow + Credentials + youtube.v3 build.
- Two clear errors: ClientSecretsMissingError vs AuthorizationRequiredError.
- fetch_liked_videos resolves the user's liked playlist id from
  channels.contentDetails.relatedPlaylists.likes (1 quota unit) and
  falls back to the well-known 'LL' magic id only if the channel
  resource doesn't expose it. Then paginates playlistItems.list.
- Tests use fake API resource via subclass override (parallel to
  ytmusic_client's _FakeClient pattern); regression test pins the
  resolved-id path."
```

---

## Task 5: Snapshot ingest refactor — TRANSLATORS dispatch + YouTube translator

Refactor `snapshot.py` so the source-specific dict-translation step becomes a registry of translators keyed by source. Add the YouTube translator that calls the classifier from Task 2 and writes into the new SnapshotItem columns from Task 3.

**Files:**
- Modify: `src/likesurgeon/snapshot.py`
- Modify: `tests/test_snapshot.py`

- [ ] **Step 1: Refactor `snapshot.py` with TRANSLATORS dispatch + youtube translator**

Replace `src/likesurgeon/snapshot.py` with:

```python
"""Snapshot ingestion and retrieval.

This module is the only writer of ``Track`` and ``Snapshot`` rows. Callers
hand it raw provider dicts; ``_TRANSLATORS`` dispatches to the source's
translator, which normalizes the dict into a ``record`` shape that
``_upsert_track`` and ``create_snapshot`` consume.

**Point-in-time guarantee.** ``Track`` holds the *latest* known metadata
(overwritten on every scan), but ``SnapshotItem`` freezes the values seen
at scan time — including the music-candidate classifier output for sources
that aren't guaranteed-music (i.e. youtube_liked_videos).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .classify import classify
from .models import Snapshot, SnapshotItem, Track
from .normalize import canonical_key

YTMUSIC_LIKED_SONGS = "ytmusic_liked_songs"
YOUTUBE_LIKED_VIDEOS = "youtube_liked_videos"


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


def _ytmusic_to_record(item: dict[str, Any]) -> dict[str, Any]:
    """Translate one ytmusicapi liked-song dict to a normalized track record."""
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
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": album,
        "duration_seconds": duration_seconds,
        "thumbnails": item.get("thumbnails"),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": item,
        "is_music_candidate": None,  # ytmusic source: every track is music
        "music_candidate_score": None,
        "music_candidate_reason": None,
    }


def _youtube_to_record(item: dict[str, Any]) -> dict[str, Any]:
    """Translate one YouTube playlistItems.list entry to a track record.

    The YouTube API shape:
      {
        "snippet": {
            "title": ..., "channelTitle": ..., "description": ...,
            "thumbnails": {...}, "publishedAt": ...,
            "resourceId": {"videoId": ...},
        },
        "contentDetails": {"videoId": ..., "videoPublishedAt": ...},
        ...
      }
    Channel name maps to ``artists=[channel]`` so cross-source matching can
    use the same canonical key shape. ``description`` and ``publishedAt``
    survive in ``raw_json`` but don't get top-level columns.
    """
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}

    video_id = (
        content_details.get("videoId")
        or (snippet.get("resourceId") or {}).get("videoId")
    )
    title = str(snippet.get("title") or "")
    channel = str(snippet.get("channelTitle") or "").strip()
    artists = [channel] if channel else []
    description = snippet.get("description") or ""

    canon = canonical_key(title, artists)
    dedupe = video_id or f"canon:{canon}"

    classification = classify(title=title, channel=channel, description=description)

    return {
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": None,
        "duration_seconds": None,
        "thumbnails": (snippet.get("thumbnails") or None),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": item,
        "is_music_candidate": classification.is_music_candidate,
        "music_candidate_score": classification.score,
        "music_candidate_reason": classification.reason,
    }


_TRANSLATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    YTMUSIC_LIKED_SONGS: _ytmusic_to_record,
    YOUTUBE_LIKED_VIDEOS: _youtube_to_record,
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
    if source not in _TRANSLATORS:
        raise ValueError(
            f"Unknown source {source!r}; expected one of {sorted(_TRANSLATORS)}"
        )
    translator = _TRANSLATORS[source]

    items_list = list(items)
    snapshot = Snapshot(source=source, created_at=_utcnow(), raw_count=len(items_list))
    session.add(snapshot)
    session.flush()
    for position, item in enumerate(items_list):
        rec = translator(item)
        track = _upsert_track(session, source, rec)
        session.add(
            SnapshotItem(
                snapshot_id=snapshot.id,
                track_id=track.id,
                position=position,
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
                is_music_candidate=rec["is_music_candidate"],
                music_candidate_score=rec["music_candidate_score"],
                music_candidate_reason=rec["music_candidate_reason"],
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
    """Return point-in-time snapshot rows in scan order, with master Track eager-loaded."""
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

- [ ] **Step 2: Append YouTube translator tests to `tests/test_snapshot.py`**

Append AFTER the existing test (do not modify the existing test):

```python
def test_create_snapshot_unknown_source_raises(session: Session):
    import pytest

    with pytest.raises(ValueError, match="Unknown source"):
        create_snapshot(session, "spotify_likes", [])


def test_youtube_translator_classifies_music_video(session: Session):
    """A YouTube playlistItem with 'Official Music Video' in title should
    land with is_music_candidate=True and a score recording the signal."""
    item = {
        "snippet": {
            "title": "Song Name (Official Music Video)",
            "channelTitle": "ArtistVEVO",
            "description": "",
            "resourceId": {"videoId": "yt1"},
            "thumbnails": {},
        },
        "contentDetails": {"videoId": "yt1"},
    }
    snap = create_snapshot(session, "youtube_liked_videos", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert len(items) == 1
    it = items[0]
    assert it.video_id == "yt1"
    assert it.is_music_candidate is True
    assert it.music_candidate_score is not None
    assert it.music_candidate_score >= 2
    assert it.music_candidate_reason is not None
    # Channel name went into artists.
    import json as jsonlib

    assert jsonlib.loads(it.artists) == ["ArtistVEVO"]


def test_youtube_translator_marks_vlog_as_not_music(session: Session):
    item = {
        "snippet": {
            "title": "Daily vlog #42",
            "channelTitle": "VlogChannel",
            "description": "",
            "resourceId": {"videoId": "yt2"},
        },
        "contentDetails": {"videoId": "yt2"},
    }
    snap = create_snapshot(session, "youtube_liked_videos", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert items[0].is_music_candidate is False


def test_ytmusic_translator_leaves_classifier_columns_null(session: Session):
    """Sources that are guaranteed-music shouldn't fill classifier columns."""
    item = {
        "videoId": "v1",
        "title": "Song",
        "artists": [{"name": "Artist"}],
    }
    snap = create_snapshot(session, "ytmusic_liked_songs", [item])
    session.commit()
    items = get_snapshot_items(session, snap.id)
    assert items[0].is_music_candidate is None
    assert items[0].music_candidate_score is None
    assert items[0].music_candidate_reason is None
```

- [ ] **Step 3: Run snapshot tests**

Run: `uv run pytest tests/test_snapshot.py -v`
Expected: 5 passed (1 existing + 4 new).

- [ ] **Step 4: Run full suite**

Run: `uv run pytest`
Expected: 49 passed (45 + 4).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/likesurgeon/snapshot.py tests/test_snapshot.py`
Expected: All checks passed.

- [ ] **Step 6: End turn — commit on next turn**

```bash
git add src/likesurgeon/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): TRANSLATORS dispatch + YouTube ingest with classifier

- create_snapshot now dispatches by source via _TRANSLATORS map; ytmusic
  translator unchanged.
- _youtube_to_record handles playlistItems.list shape: channelTitle ->
  artists, resourceId.videoId / contentDetails.videoId, classifier output
  written to SnapshotItem columns.
- Unknown source raises ValueError instead of crashing on missing key.
- Tests: classifier wiring, vlog negative path, ytmusic source leaves
  classifier cols NULL."
```

---

## Task 6: CLI — `auth youtube` + `scan youtube-likes`

Wire the YouTube client into the Typer CLI. Mirror the shape of `auth ytmusic` / `scan ytmusic`.

**Files:**
- Modify: `src/likesurgeon/cli.py`

- [ ] **Step 1: Add `auth youtube` command**

Below the existing `auth_ytmusic` function in `src/likesurgeon/cli.py`, add:

```python
@auth_app.command("youtube")
def auth_youtube() -> None:
    """Set up OAuth for YouTube Data API access."""
    cfg = Config.load()
    cfg.ensure_app_dir()
    secrets_path = cfg.youtube_oauth_client_path
    token_path = cfg.youtube_token_path

    console.print("[bold]YouTube OAuth setup[/bold]")
    if not secrets_path.exists():
        console.print(
            "1. Open [cyan]https://console.cloud.google.com/[/cyan] and create "
            "(or pick) a project.\n"
            "2. Enable [bold]YouTube Data API v3[/bold] for the project.\n"
            "3. APIs & Services → OAuth consent screen → External, add yourself "
            "as a test user.\n"
            "4. Credentials → Create credentials → OAuth client ID → "
            "[bold]Desktop app[/bold] → download the JSON.\n"
            f"5. Save it as [cyan]{secrets_path}[/cyan].\n"
            "6. Re-run [cyan]uv run likesurgeon auth youtube[/cyan]."
        )
        return

    from .youtube_client import YouTubeClient

    client = YouTubeClient(
        client_secrets_path=secrets_path,
        token_path=token_path,
    )
    console.print(
        "[dim]Opening browser for Google consent…[/dim] "
        "(this will block until you approve and the local server captures the redirect)."
    )
    client.authorize()
    console.print(
        f"[green]✓[/green] Authorized. Token saved to [cyan]{token_path}[/cyan]."
    )
```

- [ ] **Step 2: Add `scan youtube-likes` command**

Below the existing `scan_ytmusic` function, add:

```python
@scan_app.command("youtube-likes")
def scan_youtube_likes(
    limit: Annotated[
        int, typer.Option(help="Maximum number of liked videos to fetch.")
    ] = 5000,
) -> None:
    """Fetch YouTube liked videos (LL playlist) and store a snapshot."""
    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
    )

    cfg, factory = _bootstrap()
    client = YouTubeClient(
        client_secrets_path=cfg.youtube_oauth_client_path,
        token_path=cfg.youtube_token_path,
    )
    try:
        items = client.fetch_liked_videos(limit=limit)
    except (ClientSecretsMissingError, AuthorizationRequiredError) as e:
        _fail(str(e), code=2)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "youtube_liked_videos", items)
        # Quick music-candidate count for the post-scan summary.
        from .snapshot import get_snapshot_items

        scan_items = get_snapshot_items(session, snap.id)
        music_like = sum(1 for it in scan_items if it.is_music_candidate)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} videos, [bold]{music_like}[/bold] music-like)."
        )
```

- [ ] **Step 3: Verify CLI wiring**

Run:
```bash
uv run likesurgeon auth --help
uv run likesurgeon scan --help
```

Expected output for `auth --help` lists both `ytmusic` and `youtube`. Expected output for `scan --help` lists both `ytmusic` and `youtube-likes`.

- [ ] **Step 4: Verify missing-secrets path**

Run:
```bash
TMP=/tmp/ls-task6-$$
LIKE_SURGEON_HOME=$TMP uv run likesurgeon init
LIKE_SURGEON_HOME=$TMP uv run likesurgeon auth youtube
LIKE_SURGEON_HOME=$TMP uv run likesurgeon scan youtube-likes; echo "exit=$?"
```

Expected:
- `init` prints the resolved app dir.
- `auth youtube` prints the 6-step instructions because no `youtube-oauth-client.json` exists yet.
- `scan youtube-likes` exits with `exit=2` and prints `ClientSecretsMissingError`-style message.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: 49 passed (no new tests; we already cover this via Task 4's tests).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/cli.py`
Expected: All checks passed.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/cli.py
git commit -m "feat(cli): auth youtube and scan youtube-likes commands

- auth youtube prints client_secrets setup steps when none exists; otherwise
  runs InstalledAppFlow.run_local_server and persists the token.
- scan youtube-likes paginates LL via YouTubeClient and stores a snapshot
  under source=youtube_liked_videos. Summary line includes music-candidate
  count from the classifier."
```

---

## Task 7: Comparison engine (TDD)

Pure-functional, in-memory. No DB. Three-stage match: video_id → canonical_key → RapidFuzz. Returns a `CompareResult` with categorized lists.

**Files:**
- Create: `tests/test_compare.py`
- Create: `src/likesurgeon/compare.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compare.py
"""Tests for the cross-source compare engine."""

from __future__ import annotations

from dataclasses import dataclass

from likesurgeon.compare import (
    CompareInput,
    MatchKind,
    compare_likes,
)


@dataclass(frozen=True)
class _FakeItem:
    """A minimal stand-in for SnapshotItem with the fields compare_likes reads."""

    track_id: int
    video_id: str | None
    title: str
    artists: tuple[str, ...]
    canonical_key: str
    is_music_candidate: bool | None = None


def _yt(track_id, video_id, title, artists, music=True):
    return _FakeItem(
        track_id=track_id,
        video_id=video_id,
        title=title,
        artists=tuple(artists),
        canonical_key=f"{', '.join(sorted(a.lower() for a in artists))}|{title.lower()}",
        is_music_candidate=music,
    )


def _ytm(track_id, video_id, title, artists):
    return _FakeItem(
        track_id=track_id,
        video_id=video_id,
        title=title,
        artists=tuple(artists),
        canonical_key=f"{', '.join(sorted(a.lower() for a in artists))}|{title.lower()}",
        is_music_candidate=None,  # ytmusic source: classifier not run
    )


def test_video_id_exact_match():
    ytm = [_ytm(1, "v1", "Song A", ["X"])]
    yt = [_yt(2, "v1", "Song A (Official MV)", ["X"])]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    m = res.matched[0]
    assert m.kind is MatchKind.VIDEO_ID
    assert m.ytmusic_track_id == 1
    assert m.youtube_track_id == 2


def test_canonical_key_match_when_video_id_differs():
    ytm = [_ytm(1, "vmusic", "Song A", ["X"])]
    yt = [_yt(2, "vyoutube", "song a", ["x"])]  # canonical key matches
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.CANONICAL_KEY


def test_fuzzy_match_when_neither_video_id_nor_canonical_match():
    """RapidFuzz fallback for slight misspellings / decoration."""
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine - John Lennon", ["RandomChannel"])]
    res = compare_likes(
        CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80)
    )
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.FUZZY
    assert res.matched[0].confidence >= 0.8


def test_possibly_missing_from_ytmusic_when_yt_video_unmatched():
    """Music-candidate YT videos with no match anywhere → possibly_missing_from_ytmusic."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "vy", "Cover Song (Official Audio)", ["Cover Artist"])]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.matched == []
    assert len(res.possibly_missing_from_ytmusic) == 1


def test_non_music_youtube_videos_excluded():
    """Non-music YT likes shouldn't appear in any output bucket."""
    ytm: list[_FakeItem] = []
    yt = [_yt(2, "vy", "Some random vlog", ["VlogChannel"], music=False)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.matched == []
    assert res.possibly_missing_from_ytmusic == []
    assert res.ytmusic_only_likes == []
    assert res.pointer_drift_candidates == []


def test_pointer_drift_candidates_collect_fuzzy_matches():
    """Fuzzy matches should also surface as pointer_drift_candidates."""
    ytm = [_ytm(1, "vm", "Imagine", ["John Lennon"])]
    yt = [_yt(2, "vy", "Imagine - John Lennon", ["RandomChannel"])]
    res = compare_likes(
        CompareInput(ytmusic=ytm, youtube=yt, fuzzy_threshold=80)
    )
    assert len(res.pointer_drift_candidates) == 1
    assert res.pointer_drift_candidates[0].ytmusic_track_id == 1


def test_duplicate_ytmusic_rows_yield_surplus_in_ytmusic_only_likes():
    """0.1 lets a snapshot keep duplicate rows for the same identity.
    When YT Music has two such rows and YouTube has one, only ONE pair
    matches; the surplus YT Music row falls into ytmusic_only_likes.
    Pre-row-identity bug: the surplus would silently disappear because
    ``used_ytm`` was keyed on track_id."""
    ytm = [
        _ytm(1, "v1", "A", ["X"]),
        _ytm(1, "v1", "A", ["X"]),  # duplicate row, identical identity
    ]
    yt = [_yt(2, "v1", "A (Official MV)", ["X"], music=True)]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID
    assert len(res.ytmusic_only_likes) == 1
    assert res.possibly_missing_from_ytmusic == []


def test_duplicate_youtube_rows_yield_surplus_in_possibly_missing():
    """The other direction: YouTube has the same music identity twice,
    YT Music has it once. One match, one surplus YT row in
    possibly_missing_from_ytmusic."""
    ytm = [_ytm(1, "v1", "A", ["X"])]
    yt = [
        _yt(2, "v1", "A (Official MV)", ["X"], music=True),
        _yt(2, "v1", "A (Official MV)", ["X"], music=True),  # duplicate
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID
    assert len(res.possibly_missing_from_ytmusic) == 1
    assert res.ytmusic_only_likes == []


def test_handles_json_encoded_artists_from_snapshot_item():
    """SnapshotItem stores artists as a JSON-encoded string. compare_likes
    must accept that shape directly without an adapter step in the CLI."""
    import json as _json
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _SnapshotItemish:
        track_id: int
        video_id: str | None
        title: str
        artists: str  # JSON-encoded list, like real SnapshotItem.artists
        canonical_key: str
        is_music_candidate: bool | None

    ytm = [
        _SnapshotItemish(
            track_id=1,
            video_id="v1",
            title="Song A",
            artists=_json.dumps(["X"]),
            canonical_key="x|song a",
            is_music_candidate=None,
        )
    ]
    yt = [
        _SnapshotItemish(
            track_id=2,
            video_id="v1",
            title="Song A (Official MV)",
            artists=_json.dumps(["X"]),
            canonical_key="x|song a",
            is_music_candidate=True,
        )
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert len(res.matched) == 1
    assert res.matched[0].kind is MatchKind.VIDEO_ID


def test_counts_summary():
    ytm = [
        _ytm(1, "v1", "A", ["X"]),
        _ytm(2, "v2", "B", ["Y"]),
    ]
    yt = [
        _yt(3, "v1", "A (Official MV)", ["X"], music=True),
        _yt(4, "vN", "Random vlog", ["Vlog"], music=False),
        _yt(5, "vK", "Some music I love", ["RandomChannel"], music=True),
    ]
    res = compare_likes(CompareInput(ytmusic=ytm, youtube=yt))
    assert res.ytmusic_count == 2
    assert res.youtube_total_count == 3
    assert res.youtube_music_count == 2
    assert len(res.matched) == 1  # v1 matched by video_id
    assert len(res.possibly_missing_from_ytmusic) == 1  # vK has no ytmusic match
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compare.py -v`
Expected: all fail with `ModuleNotFoundError: No module named 'likesurgeon.compare'`.

- [ ] **Step 3: Implement `src/likesurgeon/compare.py`**

```python
"""Cross-source comparison: ytmusic_liked_songs vs. youtube_liked_videos.

Pure-functional. Takes two lists of "items" (anything with the duck-typed
attributes ``track_id``, ``video_id``, ``title``, ``artists``,
``canonical_key``, and ``is_music_candidate``) and returns a structured
result. The CLI passes ``SnapshotItem`` rows directly (whose ``artists``
is a JSON-encoded string); tests pass a lightweight stand-in with
list/tuple ``artists``. ``_normalize_artists`` accepts either shape.

Three-stage matching, in order of confidence:
    1. ``video_id`` exact (highest confidence)
    2. ``canonical_key`` exact (decoration-robust)
    3. RapidFuzz fuzzy on ``"<title> | <artists>"`` (last resort)

**Multiset semantics.** A snapshot may contain duplicate rows for the same
``track_id`` (Task 6 policy in 0.1: "scans are recorded faithfully"). The
matcher pairs *rows* — not *track ids* — so if YT Music has two rows for
identity X and YouTube has one, exactly one match is reported and the
surplus YT Music row falls into ``ytmusic_only_likes``. Identity tracking
uses each input list's enumerate index internally, so callers don't need
to expose row-level ids on their items.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from rapidfuzz import fuzz


class _Itemish(Protocol):
    track_id: int
    video_id: str | None
    title: str
    # ``artists`` is JSON-encoded ``str`` when this is a ``SnapshotItem`` and
    # a plain ``list``/``tuple`` of strings in the test stand-in.
    # ``_normalize_artists`` decodes both.
    artists: Any
    canonical_key: str
    is_music_candidate: bool | None


def _normalize_artists(value: Any) -> list[str]:
    """Return a flat ``list[str]`` regardless of source-shape.

    Accepts:
      - JSON-encoded strings (e.g. ``'["X", "Y"]'`` from ``SnapshotItem.artists``)
      - already-decoded ``list``/``tuple`` of strings (test stand-in)
      - ``None`` / falsy → ``[]``
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in decoded] if isinstance(decoded, list) else []
    return [str(v) for v in value]


class MatchKind(str, Enum):
    VIDEO_ID = "video_id"
    CANONICAL_KEY = "canonical_key"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class Match:
    ytmusic_track_id: int
    youtube_track_id: int
    kind: MatchKind
    confidence: float  # 1.0 for exact stages; 0.0–1.0 for fuzzy
    ytmusic_title: str
    youtube_title: str


@dataclass(frozen=True)
class UnmatchedItem:
    track_id: int
    video_id: str | None
    title: str
    artists: list[str]
    canonical_key: str


@dataclass(frozen=True)
class CompareInput:
    ytmusic: list[_Itemish]
    youtube: list[_Itemish]
    fuzzy_threshold: int = 85  # RapidFuzz score 0–100; >= threshold counts


@dataclass(frozen=True)
class CompareResult:
    ytmusic_count: int
    youtube_total_count: int
    youtube_music_count: int

    matched: list[Match] = field(default_factory=list)
    # Music-candidate YT videos with no match in YT Music — i.e. likes the
    # user expressed on YouTube that aren't represented on YouTube Music.
    # This is the high-priority bucket: the "backup half" of like-surgeon.
    possibly_missing_from_ytmusic: list[UnmatchedItem] = field(default_factory=list)
    # YT Music tracks with no corresponding YT video. Lower priority — most
    # users don't mirror every YT Music like to YouTube. Surfaced for
    # completeness; ``issues --type ytmusic_only_likes`` filters these out.
    ytmusic_only_likes: list[UnmatchedItem] = field(default_factory=list)
    # Subset of ``matched``: rows that matched only via fuzzy stage.
    pointer_drift_candidates: list[Match] = field(default_factory=list)


def _to_unmatched(item: _Itemish) -> UnmatchedItem:
    return UnmatchedItem(
        track_id=item.track_id,
        video_id=item.video_id,
        title=item.title,
        artists=_normalize_artists(item.artists),
        canonical_key=item.canonical_key,
    )


def _fuzz_target(item: _Itemish) -> str:
    artists = _normalize_artists(item.artists)
    return f"{item.title} | {', '.join(artists)}"


def compare_likes(inp: CompareInput) -> CompareResult:
    """Three-stage matcher with row-level identity.

    Each input *row* (tracked via the input list's enumerate index) is
    matched at most once, so duplicate rows in either source — same
    ``track_id``, same ``video_id``, same ``canonical_key`` — produce
    surplus that lands in the unmatched buckets rather than getting
    silently collapsed.
    """
    # Music-only YT candidates participate in matching, but the original
    # positions are preserved so non-music YT rows still count toward
    # ``youtube_total_count``.
    youtube_music: list[tuple[int, _Itemish]] = [
        (idx, it) for idx, it in enumerate(inp.youtube) if it.is_music_candidate
    ]

    matched: list[Match] = []
    used_ytm_idx: set[int] = set()
    used_yt_idx: set[int] = set()

    def _take_first_unused(candidates: list[int]) -> int | None:
        return next((i for i in candidates if i not in used_ytm_idx), None)

    def _record_match(
        yt_idx: int, ytm_idx: int, kind: MatchKind, confidence: float
    ) -> None:
        ytm = inp.ytmusic[ytm_idx]
        yt = inp.youtube[yt_idx]
        matched.append(
            Match(
                ytmusic_track_id=ytm.track_id,
                youtube_track_id=yt.track_id,
                kind=kind,
                confidence=confidence,
                ytmusic_title=ytm.title,
                youtube_title=yt.title,
            )
        )
        used_ytm_idx.add(ytm_idx)
        used_yt_idx.add(yt_idx)

    # Stage 1: video_id exact match. Index is a queue per video_id so two
    # ytm rows with the same video_id can pair against two distinct yt rows.
    by_video_id_ytm: dict[str, list[int]] = {}
    for idx, it in enumerate(inp.ytmusic):
        if it.video_id:
            by_video_id_ytm.setdefault(it.video_id, []).append(idx)

    for yt_idx, yt in youtube_music:
        if yt_idx in used_yt_idx or not yt.video_id:
            continue
        candidates = by_video_id_ytm.get(yt.video_id)
        if not candidates:
            continue
        chosen = _take_first_unused(candidates)
        if chosen is None:
            continue
        _record_match(yt_idx, chosen, MatchKind.VIDEO_ID, 1.0)

    # Stage 2: canonical_key exact match — same queue-per-key shape.
    by_canon_ytm: dict[str, list[int]] = {}
    for idx, it in enumerate(inp.ytmusic):
        by_canon_ytm.setdefault(it.canonical_key, []).append(idx)

    for yt_idx, yt in youtube_music:
        if yt_idx in used_yt_idx:
            continue
        candidates = by_canon_ytm.get(yt.canonical_key)
        if not candidates:
            continue
        chosen = _take_first_unused(candidates)
        if chosen is None:
            continue
        _record_match(yt_idx, chosen, MatchKind.CANONICAL_KEY, 1.0)

    # Stage 3: RapidFuzz fuzzy on "title | artists". Best-of-remaining per
    # yt row, ties go to the highest score. Each ytm row reserved on use.
    threshold = inp.fuzzy_threshold
    for yt_idx, yt in youtube_music:
        if yt_idx in used_yt_idx:
            continue
        best: tuple[float, int] | None = None
        for ytm_idx, ytm in enumerate(inp.ytmusic):
            if ytm_idx in used_ytm_idx:
                continue
            score = fuzz.token_set_ratio(_fuzz_target(yt), _fuzz_target(ytm))
            if score >= threshold and (best is None or score > best[0]):
                best = (float(score), ytm_idx)
        if best is not None:
            score, ytm_idx = best
            _record_match(yt_idx, ytm_idx, MatchKind.FUZZY, score / 100.0)

    # Build unmatched lists at row level — surplus rows survive intact.
    possibly_missing = [
        _to_unmatched(yt)
        for yt_idx, yt in youtube_music
        if yt_idx not in used_yt_idx
    ]
    ytmusic_only = [
        _to_unmatched(ytm)
        for ytm_idx, ytm in enumerate(inp.ytmusic)
        if ytm_idx not in used_ytm_idx
    ]
    pointer_drift = [m for m in matched if m.kind is MatchKind.FUZZY]

    return CompareResult(
        ytmusic_count=len(inp.ytmusic),
        youtube_total_count=len(inp.youtube),
        youtube_music_count=len(youtube_music),
        matched=matched,
        possibly_missing_from_ytmusic=possibly_missing,
        ytmusic_only_likes=ytmusic_only,
        pointer_drift_candidates=pointer_drift,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_compare.py -v`
Expected: 10 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: 59 passed (49 + 10).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/compare.py tests/test_compare.py`
Expected: All checks passed.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/compare.py tests/test_compare.py
git commit -m "feat(compare): three-stage cross-source match with row-level multiset

- Pure compare_likes(CompareInput) -> CompareResult.
- Stages: video_id exact, canonical_key exact, RapidFuzz fuzzy on
  'title | artists'. Each input *row* (tracked via enumerate index)
  is matched at most once; duplicate identities in either source
  surface as surplus in the unmatched buckets instead of silently
  collapsing — matches the multiset policy that Snapshot/diff uses.
- Buckets: matched, possibly_missing_from_ytmusic (high-priority),
  ytmusic_only_likes (low-priority info), pointer_drift_candidates
  (subset of matched, kind=FUZZY). Naming reflects direction so
  'missing_from_X' means 'X doesn't have it' end-to-end.
- Non-music YT items are filtered out before matching.
- Two regression tests pin the duplicate-row behavior in both
  directions."
```

---

## Task 8: Diagnosis service + `compare-likes` CLI

Persist a comparison run as a `Diagnosis` row plus `DiagnosisItem` rows. Wire the CLI so `compare-likes` runs the comparison, stores it, and prints a Rich summary.

**Files:**
- Create: `src/likesurgeon/diagnosis.py`
- Create: `tests/test_diagnosis.py`
- Modify: `src/likesurgeon/cli.py`

- [ ] **Step 1: Write `src/likesurgeon/diagnosis.py`**

```python
"""Diagnosis persistence: turn CompareResult into Diagnosis + DiagnosisItem rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .compare import CompareResult, Match, MatchKind, UnmatchedItem
from .models import Diagnosis, DiagnosisItem

ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC = "possibly_missing_from_ytmusic"
ISSUE_POINTER_DRIFT = "possible_pointer_drift"
ISSUE_YTMUSIC_ONLY = "ytmusic_only"


@dataclass(frozen=True)
class DiagnosisInput:
    ytmusic_snapshot_id: int | None
    youtube_snapshot_id: int | None
    result: CompareResult


def create_diagnosis(session: Session, inp: DiagnosisInput) -> Diagnosis:
    """Persist a Diagnosis row plus one DiagnosisItem per finding."""
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=inp.ytmusic_snapshot_id,
        youtube_snapshot_id=inp.youtube_snapshot_id,
    )
    session.add(diagnosis)
    session.flush()

    # High-priority bucket: the user liked it on YouTube but YT Music doesn't
    # have it — these are the candidates the "backup" half of the project
    # should surface most loudly.
    for unmatched in inp.result.possibly_missing_from_ytmusic:
        session.add(
            _unmatched_item(
                diagnosis.id,
                unmatched,
                ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
                0.9,
            )
        )

    for match in inp.result.pointer_drift_candidates:
        session.add(_match_item(diagnosis.id, match, ISSUE_POINTER_DRIFT))

    # Low-priority informational bucket: YT Music has it, YouTube doesn't.
    # Often this is just "user never liked it on YouTube" rather than a
    # problem, so the confidence is intentionally low.
    for unmatched in inp.result.ytmusic_only_likes:
        session.add(
            _unmatched_item(
                diagnosis.id,
                unmatched,
                ISSUE_YTMUSIC_ONLY,
                0.5,
            )
        )

    session.flush()
    return diagnosis


def _unmatched_item(
    diagnosis_id: int,
    item: UnmatchedItem,
    issue_type: str,
    confidence: float,
) -> DiagnosisItem:
    return DiagnosisItem(
        diagnosis_id=diagnosis_id,
        issue_type=issue_type,
        confidence=confidence,
        reason=_unmatched_reason(item),
        source_track_id=item.track_id,
        related_track_id=None,
        status="open",
    )


def _match_item(
    diagnosis_id: int,
    match: Match,
    issue_type: str,
) -> DiagnosisItem:
    return DiagnosisItem(
        diagnosis_id=diagnosis_id,
        issue_type=issue_type,
        confidence=match.confidence,
        reason=(
            f"fuzzy match (score {match.confidence * 100:.0f}/100): "
            f"YT '{match.youtube_title}' ↔ YT Music '{match.ytmusic_title}'"
        ),
        source_track_id=match.youtube_track_id,
        related_track_id=match.ytmusic_track_id,
        status="open",
    )


def _unmatched_reason(item: UnmatchedItem) -> str:
    artists = ", ".join(item.artists) if item.artists else "(no artists)"
    return f"'{item.title}' by {artists}"


def latest_diagnosis(session: Session) -> Diagnosis | None:
    stmt = (
        select(Diagnosis).order_by(Diagnosis.created_at.desc(), Diagnosis.id.desc()).limit(1)
    )
    return session.scalar(stmt)


def diagnosis_items(session: Session, diagnosis_id: int) -> list[DiagnosisItem]:
    stmt = (
        select(DiagnosisItem)
        .where(DiagnosisItem.diagnosis_id == diagnosis_id)
        .order_by(DiagnosisItem.confidence.desc(), DiagnosisItem.id)
    )
    return list(session.scalars(stmt).all())
```

- [ ] **Step 2: Write `tests/test_diagnosis.py`**

```python
"""Tests for diagnosis persistence.

These tests use real ``create_snapshot`` calls (rather than fake Track ids)
so that the ``DiagnosisItem.source_track_id`` / ``related_track_id`` foreign
keys resolve under FK enforcement (Task 1 enabled ``PRAGMA foreign_keys=ON``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import (
    CompareResult,
    Match,
    MatchKind,
    UnmatchedItem,
)
from likesurgeon.diagnosis import (
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_YTMUSIC_ONLY,
    DiagnosisInput,
    create_diagnosis,
    diagnosis_items,
    latest_diagnosis,
)
from likesurgeon.snapshot import create_snapshot, get_snapshot_items


def _ytm_item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def _yt_item(video_id: str, title: str, channel: str = "ArtistVEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


def _summary(item) -> UnmatchedItem:
    import json as _json

    return UnmatchedItem(
        track_id=item.track_id,
        video_id=item.video_id,
        title=item.title,
        artists=_json.loads(item.artists) if item.artists else [],
        canonical_key=item.canonical_key,
    )


def test_create_diagnosis_persists_each_bucket(session: Session):
    """Real Track rows, real ids in the synthetic CompareResult, FK happy."""
    ytm_snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("v_match", "Imagine", ["Lennon"]),
            _ytm_item("v_ytm_only", "YT Music Only", ["Artist"]),
        ],
    )
    yt_snap = create_snapshot(
        session,
        "youtube_liked_videos",
        [
            _yt_item("v_yt_match", "Imagine - John Lennon (Official MV)", "Lennon"),
            _yt_item("v_yt_only", "Cover Song (Official Audio)", "CoverArtist"),
        ],
    )
    session.commit()

    ytm_items = get_snapshot_items(session, ytm_snap.id)
    yt_items = get_snapshot_items(session, yt_snap.id)

    drift_match = Match(
        ytmusic_track_id=ytm_items[0].track_id,
        youtube_track_id=yt_items[0].track_id,
        kind=MatchKind.FUZZY,
        confidence=0.88,
        ytmusic_title=ytm_items[0].title,
        youtube_title=yt_items[0].title,
    )

    result = CompareResult(
        ytmusic_count=2,
        youtube_total_count=2,
        youtube_music_count=2,
        matched=[drift_match],
        possibly_missing_from_ytmusic=[_summary(yt_items[1])],
        ytmusic_only_likes=[_summary(ytm_items[1])],
        pointer_drift_candidates=[drift_match],
    )

    diag = create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=result,
        ),
    )
    session.commit()

    items = diagnosis_items(session, diag.id)
    by_type: dict[str, list] = {}
    for it in items:
        by_type.setdefault(it.issue_type, []).append(it)

    assert ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC in by_type
    assert ISSUE_POINTER_DRIFT in by_type
    assert ISSUE_YTMUSIC_ONLY in by_type
    assert len(by_type[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]) == 1
    assert len(by_type[ISSUE_POINTER_DRIFT]) == 1
    assert len(by_type[ISSUE_YTMUSIC_ONLY]) == 1
    # Pointer-drift item should reference both tracks (real ids).
    drift = by_type[ISSUE_POINTER_DRIFT][0]
    assert drift.source_track_id == yt_items[0].track_id
    assert drift.related_track_id == ytm_items[0].track_id
    # All items default to status='open'.
    assert all(it.status == "open" for it in items)


def test_latest_diagnosis_orders_by_recency(session: Session):
    """Two empty diagnoses on empty snapshots; latest returns the newer one."""
    ytm_snap = create_snapshot(session, "ytmusic_liked_songs", [])
    yt_snap = create_snapshot(session, "youtube_liked_videos", [])
    session.commit()

    empty = CompareResult(
        ytmusic_count=0, youtube_total_count=0, youtube_music_count=0
    )
    inp = DiagnosisInput(
        ytmusic_snapshot_id=ytm_snap.id,
        youtube_snapshot_id=yt_snap.id,
        result=empty,
    )
    first = create_diagnosis(session, inp)
    session.commit()
    second = create_diagnosis(session, inp)
    session.commit()
    assert latest_diagnosis(session).id == second.id
    assert second.id != first.id
```

- [ ] **Step 3: Add `compare-likes` to `cli.py`**

In `src/likesurgeon/cli.py`, add this function after `doctor`:

```python
@app.command("compare-likes")
def compare_likes_cmd() -> None:
    """Compare latest YouTube Music vs. YouTube liked-videos snapshots."""
    from .compare import CompareInput, compare_likes
    from .diagnosis import DiagnosisInput, create_diagnosis
    from .snapshot import get_snapshot_items, latest_snapshot

    _, factory = _bootstrap()
    with session_scope(factory) as session:
        ytm_snap = latest_snapshot(session, source="ytmusic_liked_songs")
        yt_snap = latest_snapshot(session, source="youtube_liked_videos")
        if ytm_snap is None or yt_snap is None:
            missing: list[str] = []
            if ytm_snap is None:
                missing.append("[cyan]likesurgeon scan ytmusic[/cyan]")
            if yt_snap is None:
                missing.append("[cyan]likesurgeon scan youtube-likes[/cyan]")
            _fail(
                "Need both a ytmusic_liked_songs and a youtube_liked_videos "
                f"snapshot first. Run: {', '.join(missing)}.",
                code=2,
            )

        ytm_items = get_snapshot_items(session, ytm_snap.id)
        yt_items = get_snapshot_items(session, yt_snap.id)

        result = compare_likes(CompareInput(ytmusic=ytm_items, youtube=yt_items))
        diag = create_diagnosis(
            session,
            DiagnosisInput(
                ytmusic_snapshot_id=ytm_snap.id,
                youtube_snapshot_id=yt_snap.id,
                result=result,
            ),
        )

    console.print(f"[green]✓[/green] Diagnosis [bold]#{diag.id}[/bold] saved.")
    table = Table(title="compare-likes summary")
    table.add_column("Bucket")
    table.add_column("Count", justify="right")
    table.add_row("YouTube Music liked songs", str(result.ytmusic_count))
    table.add_row("YouTube liked videos (total)", str(result.youtube_total_count))
    table.add_row("YouTube liked videos (music-like)", str(result.youtube_music_count))
    table.add_row("Matched (any stage)", str(len(result.matched)))
    table.add_row(
        "Possibly missing from YT Music",
        str(len(result.possibly_missing_from_ytmusic)),
    )
    table.add_row(
        "YT Music only (not liked on YouTube)",
        str(len(result.ytmusic_only_likes)),
    )
    table.add_row("Pointer-drift candidates", str(len(result.pointer_drift_candidates)))
    console.print(table)
    console.print(
        "Run [cyan]likesurgeon issues[/cyan] for the full per-item breakdown."
    )
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/test_diagnosis.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: 61 passed (59 + 2).

- [ ] **Step 6: Smoke `compare-likes` with no snapshots**

Run:
```bash
TMP=/tmp/ls-task8-$$
LIKE_SURGEON_HOME=$TMP uv run likesurgeon init
LIKE_SURGEON_HOME=$TMP uv run likesurgeon compare-likes; echo "exit=$?"
```

Expected: exits with `exit=2` and a message instructing to run scan commands.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/likesurgeon/diagnosis.py src/likesurgeon/cli.py tests/test_diagnosis.py`
Expected: All checks passed.

- [ ] **Step 8: End turn — commit on next turn**

```bash
git add src/likesurgeon/diagnosis.py src/likesurgeon/cli.py tests/test_diagnosis.py
git commit -m "feat(diagnosis): persist compare-likes runs; add compare-likes CLI

- Diagnosis + DiagnosisItem rows: one per finding across three issue types
  (possibly_missing_from_ytmusic, possible_pointer_drift, ytmusic_only).
- Confidence per type: 0.9 high-priority (YT-only music candidates),
  fuzzy score for drift, 0.5 informational (YT Music only).
- compare-likes CLI fails with exit 2 when either snapshot is missing,
  prints a Rich bucket summary, points users to 'issues' for details.
- Tests use real Track ids from create_snapshot so DiagnosisItem FK
  references resolve under PRAGMA foreign_keys=ON (Task 1)."
```

---

## Task 9: `issues` CLI

Read-only viewer for the latest diagnosis. Filters by `--type`, `--min-confidence`. Output as Rich table or JSON.

**Files:**
- Modify: `src/likesurgeon/cli.py`

- [ ] **Step 1: Add `issues` command**

In `src/likesurgeon/cli.py`, add after `compare_likes_cmd`:

```python
@app.command()
def issues(
    type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help=(
                "Filter by issue type "
                "(possibly_missing_from_ytmusic | possible_pointer_drift | "
                "ytmusic_only)."
            ),
        ),
    ] = None,
    min_confidence: Annotated[
        float,
        typer.Option(
            "--min-confidence",
            help="Hide items with confidence below this (0.0–1.0).",
        ),
    ] = 0.0,
    format: Annotated[
        str,
        typer.Option(
            "--format", "-f", help="Output format: 'table' (default) or 'json'."
        ),
    ] = "table",
) -> None:
    """List issues from the latest diagnosis."""
    import json as jsonlib

    from .diagnosis import diagnosis_items, latest_diagnosis

    fmt = format.lower()
    if fmt not in {"table", "json"}:
        _fail(
            f"Unsupported format: {format!r}. Use 'table' or 'json'.", code=2
        )

    _, factory = _bootstrap()
    with session_scope(factory) as session:
        diag = latest_diagnosis(session)
        if diag is None:
            console.print(
                "[yellow]No diagnosis yet.[/yellow] "
                "Run [cyan]likesurgeon compare-likes[/cyan] first."
            )
            return
        items = diagnosis_items(session, diag.id)

    if type is not None:
        items = [it for it in items if it.issue_type == type]
    items = [it for it in items if it.confidence >= min_confidence]

    if fmt == "json":
        payload = {
            "diagnosis_id": diag.id,
            "items": [
                {
                    "id": it.id,
                    "issue_type": it.issue_type,
                    "confidence": it.confidence,
                    "reason": it.reason,
                    "source_track_id": it.source_track_id,
                    "related_track_id": it.related_track_id,
                    "status": it.status,
                }
                for it in items
            ],
        }
        print(jsonlib.dumps(payload, indent=2, ensure_ascii=False))
        return

    table = Table(title=f"Diagnosis #{diag.id} — issues ({len(items)})")
    table.add_column("ID", justify="right")
    table.add_column("Type")
    table.add_column("Conf", justify="right")
    table.add_column("Source TID", justify="right")
    table.add_column("Related TID", justify="right")
    table.add_column("Reason")
    for it in items:
        table.add_row(
            str(it.id),
            it.issue_type,
            f"{it.confidence:.2f}",
            str(it.source_track_id) if it.source_track_id is not None else "",
            str(it.related_track_id) if it.related_track_id is not None else "",
            it.reason,
        )
    if not items:
        console.print("[dim]No issues match the filter.[/dim]")
        return
    console.print(table)
```

- [ ] **Step 2: Verify CLI listing**

Run: `uv run likesurgeon --help`
Expected: lists `compare-likes` and `issues` alongside the other top-level commands.

Run: `uv run likesurgeon issues --help`
Expected: shows `--type`, `--min-confidence`, `--format` options.

- [ ] **Step 3: Smoke with no diagnosis**

Run:
```bash
TMP=/tmp/ls-task9-$$
LIKE_SURGEON_HOME=$TMP uv run likesurgeon init
LIKE_SURGEON_HOME=$TMP uv run likesurgeon issues
```

Expected: prints `No diagnosis yet.` and exits 0.

- [ ] **Step 4: Smoke unsupported format**

Run:
```bash
LIKE_SURGEON_HOME=$TMP uv run likesurgeon issues --format yaml; echo "exit=$?"
```

Expected: error message, `exit=2`.

- [ ] **Step 5: Run full suite (no new tests)**

Run: `uv run pytest`
Expected: 61 passed.

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/cli.py`
Expected: All checks passed.

- [ ] **Step 7: End turn — commit on next turn**

```bash
git add src/likesurgeon/cli.py
git commit -m "feat(cli): issues command with --type / --min-confidence / --format json

Read-only viewer for the latest diagnosis. Defaults to a Rich table,
optional JSON for machine consumers. Empty-result and missing-diagnosis
paths surface friendly messages instead of empty output."
```

---

## Task 10: Doctor refactor — multi-source + health score + tests

`doctor` becomes multi-source aware. Emits a `MultiSourceHealthReport` covering both ytmusic and youtube, the latest diagnosis summary, and a simple match-rate health score.

**Files:**
- Modify: `src/likesurgeon/doctor.py`
- Modify: `src/likesurgeon/cli.py`
- Create: `tests/test_doctor.py`

- [ ] **Step 1: Replace `src/likesurgeon/doctor.py`**

```python
"""Health summary across stored snapshots and the latest diagnosis.

Read-only. No external calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .diff import DiffResult, diff_snapshots
from .models import Snapshot
from .snapshot import get_snapshot_items, latest_snapshot

YTMUSIC = "ytmusic_liked_songs"
YOUTUBE = "youtube_liked_videos"


@dataclass(frozen=True)
class SourceHealth:
    """Per-source view used inside the multi-source report."""

    source: str
    snapshot_count: int
    latest_count: int | None
    last_diff: DiffResult | None


@dataclass(frozen=True)
class DiagnosisSummary:
    diagnosis_id: int
    possibly_missing_from_ytmusic: int
    pointer_drift: int
    ytmusic_only: int


@dataclass(frozen=True)
class MultiSourceHealthReport:
    ytmusic: SourceHealth
    youtube: SourceHealth
    latest_diagnosis: DiagnosisSummary | None
    # match_rate = matched_youtube_music / max(youtube_music_count, 1) * 100.
    # ``None`` when the latest diagnosis is missing or has no music candidates.
    match_rate_percent: float | None


def _source_health(session: Session, source: str) -> SourceHealth:
    snapshot_count = (
        session.scalar(
            select(func.count(Snapshot.id)).where(Snapshot.source == source)
        )
        or 0
    )
    latest = latest_snapshot(session, source=source)
    if latest is None:
        return SourceHealth(
            source=source,
            snapshot_count=snapshot_count,
            latest_count=None,
            last_diff=None,
        )

    prev_stmt = (
        select(Snapshot)
        .where(Snapshot.source == source, Snapshot.id != latest.id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(1)
    )
    prev = session.scalar(prev_stmt)
    diff = diff_snapshots(session, prev.id, latest.id) if prev is not None else None

    return SourceHealth(
        source=source,
        snapshot_count=snapshot_count,
        latest_count=latest.raw_count,
        last_diff=diff,
    )


def _latest_diagnosis_summary(session: Session) -> tuple[DiagnosisSummary | None, float | None]:
    from .diagnosis import (
        ISSUE_POINTER_DRIFT,
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        ISSUE_YTMUSIC_ONLY,
        diagnosis_items,
        latest_diagnosis,
    )

    diag = latest_diagnosis(session)
    if diag is None:
        return None, None

    items = diagnosis_items(session, diag.id)
    counts = {
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC: 0,
        ISSUE_POINTER_DRIFT: 0,
        ISSUE_YTMUSIC_ONLY: 0,
    }
    for it in items:
        if it.issue_type in counts:
            counts[it.issue_type] += 1
    summary = DiagnosisSummary(
        diagnosis_id=diag.id,
        possibly_missing_from_ytmusic=counts[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
        pointer_drift=counts[ISSUE_POINTER_DRIFT],
        ytmusic_only=counts[ISSUE_YTMUSIC_ONLY],
    )

    # Health: match_rate = (yt_music_candidates - unmatched) / yt_music_candidates.
    # The "unmatched" count is exactly the high-priority bucket from this run.
    if diag.youtube_snapshot_id is None:
        return summary, None
    yt_items = get_snapshot_items(session, diag.youtube_snapshot_id)
    yt_music_count = sum(1 for it in yt_items if it.is_music_candidate)
    if yt_music_count == 0:
        return summary, None
    matched_music = yt_music_count - counts[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]
    return summary, max(0.0, min(100.0, matched_music / yt_music_count * 100))


def health_summary(session: Session) -> MultiSourceHealthReport:
    """Multi-source health snapshot. No source argument — always covers both."""
    summary, match_rate = _latest_diagnosis_summary(session)
    return MultiSourceHealthReport(
        ytmusic=_source_health(session, YTMUSIC),
        youtube=_source_health(session, YOUTUBE),
        latest_diagnosis=summary,
        match_rate_percent=match_rate,
    )
```

- [ ] **Step 2: Update `cli.py` `doctor` to render the new shape**

Replace the existing `doctor` function in `src/likesurgeon/cli.py` with:

```python
@app.command()
def doctor() -> None:
    """Multi-source health summary using stored data."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        report = health_summary(session)

    def _render_source(label: str, sh) -> None:
        if sh.latest_count is None:
            console.print(
                f"[yellow]{label}:[/yellow] no snapshots yet "
                "(run the matching scan command first)."
            )
            return
        line = (
            f"[bold]{label}:[/bold] {sh.latest_count} items "
            f"({sh.snapshot_count} snapshots)"
        )
        if sh.last_diff is not None:
            line += (
                f"  · vs prev: +{len(sh.last_diff.added)} / "
                f"-{len(sh.last_diff.removed)} / common {sh.last_diff.common_count}"
            )
        console.print(line)

    console.print("[bold]like-surgeon doctor[/bold]")
    _render_source("YouTube Music liked songs", report.ytmusic)
    _render_source("YouTube liked videos", report.youtube)

    if report.latest_diagnosis is None:
        console.print(
            "[dim]No diagnosis yet — run [cyan]likesurgeon compare-likes[/cyan].[/dim]"
        )
    else:
        d = report.latest_diagnosis
        console.print(
            f"[bold]Latest diagnosis #{d.diagnosis_id}:[/bold] "
            f"{d.possibly_missing_from_ytmusic} possibly missing from YT Music · "
            f"{d.pointer_drift} pointer drift · "
            f"{d.ytmusic_only} YT Music only"
        )

    if report.match_rate_percent is None:
        console.print("[dim]Match-rate health score: N/A (no music candidates).[/dim]")
    else:
        score = report.match_rate_percent
        color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
        console.print(
            f"[bold]Match-rate health score:[/bold] [{color}]{score:.1f}%[/{color}]"
        )
```

(Drop the now-unused import of `health_summary`'s old return type if any; the import line `from .doctor import health_summary` still works.)

- [ ] **Step 3: Write `tests/test_doctor.py`**

```python
"""Tests for the multi-source doctor health report."""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import (
    CompareResult,
    Match,
    MatchKind,
    UnmatchedItem,
)
from likesurgeon.diagnosis import DiagnosisInput, create_diagnosis
from likesurgeon.doctor import health_summary
from likesurgeon.snapshot import create_snapshot


def _yt(video_id: str, title: str, channel: str = "Some Music VEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


def _ytm(video_id: str, title: str, artists: list[str]) -> dict:
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a} for a in artists],
    }


def test_doctor_with_no_snapshots(session: Session):
    report = health_summary(session)
    assert report.ytmusic.latest_count is None
    assert report.youtube.latest_count is None
    assert report.latest_diagnosis is None
    assert report.match_rate_percent is None


def test_doctor_aggregates_both_sources(session: Session):
    create_snapshot(
        session,
        "ytmusic_liked_songs",
        [_ytm("v1", "A", ["X"]), _ytm("v2", "B", ["Y"])],
    )
    session.commit()
    create_snapshot(
        session,
        "youtube_liked_videos",
        [_yt("v1", "A (Official Music Video)"), _yt("v3", "C (Lyrics)")],
    )
    session.commit()

    report = health_summary(session)
    assert report.ytmusic.latest_count == 2
    assert report.youtube.latest_count == 2
    assert report.latest_diagnosis is None  # no compare-likes run yet


def test_doctor_match_rate_scores_diagnosis(session: Session):
    """Real ingest path so DiagnosisItem FKs resolve under FK enforcement.

    One YT music match (v1), one YT-only (v3) → match_rate = 1/2 = 50%.
    """
    import json as _json

    from likesurgeon.snapshot import get_snapshot_items

    ytm_snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [_ytm("v1", "A", ["X"])],
    )
    session.commit()
    yt_snap = create_snapshot(
        session,
        "youtube_liked_videos",
        [
            _yt("v1", "A (Official MV)"),  # music + matched by video_id
            _yt("v3", "C (Official Audio)"),  # music + unmatched (yt-only)
        ],
    )
    session.commit()

    ytm_items = get_snapshot_items(session, ytm_snap.id)
    yt_items = get_snapshot_items(session, yt_snap.id)

    fake_result = CompareResult(
        ytmusic_count=1,
        youtube_total_count=2,
        youtube_music_count=2,
        matched=[
            Match(
                ytmusic_track_id=ytm_items[0].track_id,
                youtube_track_id=yt_items[0].track_id,
                kind=MatchKind.VIDEO_ID,
                confidence=1.0,
                ytmusic_title=ytm_items[0].title,
                youtube_title=yt_items[0].title,
            )
        ],
        possibly_missing_from_ytmusic=[
            UnmatchedItem(
                track_id=yt_items[1].track_id,
                video_id=yt_items[1].video_id,
                title=yt_items[1].title,
                artists=_json.loads(yt_items[1].artists) if yt_items[1].artists else [],
                canonical_key=yt_items[1].canonical_key,
            )
        ],
        ytmusic_only_likes=[],
        pointer_drift_candidates=[],
    )
    create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=fake_result,
        ),
    )
    session.commit()

    report = health_summary(session)
    assert report.match_rate_percent is not None
    assert 49 <= report.match_rate_percent <= 51  # ~50%
    assert report.latest_diagnosis is not None
    assert report.latest_diagnosis.possibly_missing_from_ytmusic == 1
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest`
Expected: 64 passed (61 + 3).

- [ ] **Step 6: Lint**

Run: `uv run ruff check src/likesurgeon/doctor.py src/likesurgeon/cli.py tests/test_doctor.py`
Expected: All checks passed.

- [ ] **Step 7: Smoke `doctor` end-to-end**

Run:
```bash
TMP=/tmp/ls-task10-$$
LIKE_SURGEON_HOME=$TMP uv run likesurgeon init
LIKE_SURGEON_HOME=$TMP uv run likesurgeon doctor
```

Expected: prints "no snapshots yet" lines for both sources and "no diagnosis yet" / "N/A" for the score.

- [ ] **Step 8: End turn — commit on next turn**

```bash
git add src/likesurgeon/doctor.py src/likesurgeon/cli.py tests/test_doctor.py
git commit -m "feat(doctor): multi-source health report + match-rate score

- New MultiSourceHealthReport returns per-source SourceHealth (count,
  snapshot total, prev-diff) for both ytmusic_liked_songs and
  youtube_liked_videos.
- DiagnosisSummary surfaces counts of the three issue types from the
  latest diagnosis.
- match_rate_percent = (matched / yt_music_count) * 100, clamped 0-100.
  Returns None when no diagnosis or no music candidates."
```

---

## Task 11: README + plan amendments + final verification

**Files:**
- Modify: `README.md`
- Modify: this plan file (toggle the spec coverage check at the end)

- [ ] **Step 1: Overwrite `README.md`**

Replace the current README content with:

```markdown
# like-surgeon

> Sync, backup, and repair your YouTube Music liked songs.

**Status:** MVP 0.2 — read-only scanner across YouTube Music *and* YouTube, with cross-source diagnosis. Local-first. No server, no destructive actions.

## What it does today

- Authenticates with YouTube Music (`ytmusicapi`, browser-header) and YouTube Data API v3 (Google OAuth, desktop client).
- Snapshots `ytmusic_liked_songs` (YouTube Music likes) and `youtube_liked_videos` (YouTube LL playlist) into a local SQLite DB.
- Diffs any two snapshots, exports any snapshot to JSON.
- Classifies YouTube liked videos as music-candidate / not via heuristics (channel ends with `- Topic`, "Provided to YouTube by …", "Official Music Video", "Lyric Video", `artist - title`, etc.; negatives like `vlog`, `tutorial`, `gameplay`).
- `compare-likes` does a three-stage match (`video_id` → `canonical_key` → RapidFuzz fuzzy on `title | artists`) and persists findings as a `Diagnosis` plus per-finding `DiagnosisItem` rows.
- `issues` lists the latest diagnosis with type/confidence filters and JSON output.
- `doctor` is now multi-source: counts per source, latest diagnosis summary, and a match-rate health score.

Snapshots preserve **point-in-time metadata** — the title, channel, description, etc. that the provider returned at scan time are frozen on each `SnapshotItem`. A later rename in YouTube Music or YouTube doesn't rewrite history.

## Roadmap

| Milestone   | Scope                                                                |
|-------------|----------------------------------------------------------------------|
| 0.1         | Read-only YouTube Music liked-songs scanner + local snapshots         |
| **0.2**     | YouTube Data API + classifier + cross-source compare/issues (this)    |
| 0.3         | Matching engine for missing / "ghost" / pointer-drift tracks          |
| 0.4         | Backup playlist support                                               |
| 1.0         | Local web UI / Electron app                                           |

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

Creates `~/.like-surgeon/` and the SQLite database at `~/.like-surgeon/like-surgeon.sqlite`. Override with `LIKE_SURGEON_HOME=/some/path`.

### 2. Authenticate

**YouTube Music** (browser-header flow):

```bash
uv run likesurgeon auth ytmusic
```

This prints the exact `ytmusicapi browser` setup steps and the destination file path.

**YouTube Data API** (Google OAuth, desktop client):

```bash
uv run likesurgeon auth youtube
```

The first run prints a 6-step setup walkthrough that ends with you placing a `youtube-oauth-client.json` (downloaded from Google Cloud Console as a "Desktop app" OAuth client) into `~/.like-surgeon/`. Run the command again and it will open your browser for consent and persist the resulting refresh token to `~/.like-surgeon/youtube-token.json`.

### 3. Scan

```bash
uv run likesurgeon scan ytmusic                 # YT Music likes
uv run likesurgeon scan youtube-likes           # YouTube LL playlist
uv run likesurgeon scan youtube-likes --limit 200
```

### 4. Inspect

```bash
uv run likesurgeon snapshots
uv run likesurgeon diff <old_snapshot_id> <new_snapshot_id>
uv run likesurgeon export <snapshot_id> --format json
uv run likesurgeon export <snapshot_id> --format json --output likes.json
uv run likesurgeon doctor
```

### 5. Compare across sources

```bash
uv run likesurgeon compare-likes
uv run likesurgeon issues
uv run likesurgeon issues --type possibly_missing_from_ytmusic
uv run likesurgeon issues --min-confidence 0.7 --format json
```

`compare-likes` requires a snapshot from each source. It prints a bucket summary and persists the run as a `Diagnosis`. `issues` then surfaces the per-item breakdown.

## Upgrading from 0.1

0.2 ships breaking schema changes (source-string rename + new tables/columns). At MVP scope we don't ship in-place migrations — delete the local DB and re-scan:

```bash
rm ~/.like-surgeon/like-surgeon.sqlite
uv run likesurgeon init
uv run likesurgeon scan ytmusic
uv run likesurgeon scan youtube-likes  # once you've completed `auth youtube`
```

Re-scanning is fast (a 5000-like library is ~5 seconds for ytmusicapi and ~100 quota units for YouTube Data API).

## Caveats

- **`ytmusicapi` is community-maintained.** YouTube Music has no official public API — if a scan fails, check the [`ytmusicapi` issue tracker](https://github.com/sigma67/ytmusicapi/issues).
- **YouTube Data API quota.** A scan of 5000 likes is ~100 quota units; the default daily quota is 10000. Re-scanning a few times a day is fine.
- **Music classification is heuristic.** Edge cases will misclassify (e.g. covers labelled "tutorial"). The `compare-likes` output is a *starting point* for review, not a verdict — nothing is mutated on the user's behalf.

## Local layout

```
~/.like-surgeon/
├── like-surgeon.sqlite        # all snapshots, tracks, diagnoses
├── browser.json               # ytmusicapi browser-header auth
├── youtube-oauth-client.json  # downloaded Google OAuth client (Desktop app)
└── youtube-token.json         # persisted refresh token (created on first auth)
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

- [ ] **Step 2: Run the whole stack one final time**

```bash
cd /Users/zeikar/Developer/Projects/like-surgeon
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run likesurgeon --version
uv run likesurgeon --help
```

Expected:
- `ruff check`: All checks passed.
- `ruff format --check`: clean.
- `pytest`: **64 passed**.
- `likesurgeon --version`: `likesurgeon 0.1.0` (we'll bump to 0.2.0 in the next minor — out of scope here).
- `likesurgeon --help`: lists `init`, `auth`, `scan`, `snapshots`, `diff`, `export`, `doctor`, `compare-likes`, `issues`.

- [ ] **Step 3: End-to-end smoke**

```bash
TMP=/tmp/ls-final-0.2-$$
LIKE_SURGEON_HOME=$TMP uv run likesurgeon init
LIKE_SURGEON_HOME=$TMP uv run likesurgeon auth ytmusic 2>&1 | head -10
LIKE_SURGEON_HOME=$TMP uv run likesurgeon auth youtube 2>&1 | head -15
LIKE_SURGEON_HOME=$TMP uv run likesurgeon scan ytmusic; echo "exit=$?"        # exit 2
LIKE_SURGEON_HOME=$TMP uv run likesurgeon scan youtube-likes; echo "exit=$?"  # exit 2
LIKE_SURGEON_HOME=$TMP uv run likesurgeon compare-likes; echo "exit=$?"        # exit 2
LIKE_SURGEON_HOME=$TMP uv run likesurgeon issues
LIKE_SURGEON_HOME=$TMP uv run likesurgeon doctor
```

Expected: all auth commands print instructions; all scan/compare commands exit 2 with helpful messages; `issues` prints "no diagnosis yet"; `doctor` prints "no snapshots yet" lines + N/A score.

- [ ] **Step 4: End turn — commit on next turn**

```bash
git add README.md
git commit -m "docs: README for MVP 0.2 (YouTube Data API + cross-source compare)"
```

---

## Self-review notes (plan-only — not part of the repo)

These notes live with the plan in `docs/superpowers/plans/`. They're for the author/reviewer to track spec coverage and intentional deviations.

**Spec coverage check (against the user's MVP 0.2 prompt):**

- [x] (1) `auth youtube` + google-auth-oauthlib + google-api-python-client — Task 4 + Task 6.
- [x] (1) Local credential storage; no hardcoded secrets — Task 4 (paths in `Config`); user supplies their own `client_secrets.json`.
- [x] (1) Clear OAuth setup instructions — Task 6 `auth_youtube` 6-step walkthrough.
- [x] (2) `scan youtube-likes` — Task 6.
- [x] (2) Source name `youtube_liked_videos` — Task 5 (constant in `snapshot.py`).
- [x] (2) Stores video_id, title, channel/owner (via `artists`), description (in `raw_json`), thumbnails, published_at (in `raw_json`), raw JSON — Task 5 translator.
- [x] (2) `playlistItems.list` with the user's likes playlist — Task 4 (`_resolve_likes_playlist_id` reads `channels.contentDetails.relatedPlaylists.likes`, falls back to `"LL"`).
- [x] (2) Pagination — Task 4 (`page_token` loop).
- [x] (2) Rich post-scan summary — Task 6 (snap + music-like count).
- [x] (3) Heuristic classifier with all listed positive signals (Topic, Official Audio, Official MV, MV, M/V, Lyrics, Lyric Video, Visualizer, Provided to YouTube by, artist - title) — Task 2 `_POSITIVE_SIGNALS` + `_CHANNEL_TOPIC_RE` + `_ARTIST_DASH_TITLE_RE` + `_DESCRIPTION_PROVIDED_RE`.
- [x] (3) Negative signals (vlog, tutorial, review, reaction, gameplay, news, shorts) — Task 2 `_NEGATIVE_SIGNALS` (also adds how-to, walkthrough, unboxing, interview as related negatives).
- [x] (3) `is_music_candidate` / `music_candidate_score` / `music_candidate_reason` fields — Task 3 SnapshotItem columns.
- [x] (4) `compare-likes` command — Task 8.
- [x] (4) Three-stage matching — Task 7 (`compare.py`).
- [x] (4) Output: ytmusic count, youtube count, music-like youtube count, possibly-missing-from-ytmusic, ytmusic-only, pointer-drift — Task 7 `CompareResult` + Task 8 CLI table. (Spec's "YouTube-only music likes" and "likely missing from YouTube Music" are the same direction; we collapsed them into one bucket named `possibly_missing_from_ytmusic` for clarity, plus a separate low-priority `ytmusic_only_likes` bucket for the reverse direction.)
- [x] (4) Read-only — no mutation calls in `youtube_client` or `compare`/`diagnosis`.
- [x] (5) `diagnosis` and `diagnosis_items` tables — Task 3.
- [x] (5) Per-item `issue_type`, `confidence`, `reason`, `source_track_id`, `related_track_id`, `status='open'` — Task 3 schema + Task 8 service.
- [x] (6) `issues` command with `--type` / `--min-confidence` / `--format json` — Task 9.
- [x] (7) Improved doctor with both source counts, music-like count, latest diagnosis summary, health score — Task 10.
- [x] (8) Tests for classifier (Task 2), canonical key (existing 11 in test_normalize.py — unchanged in 0.1, so baseline coverage holds), comparison (Task 7), diagnosis (Task 8). `pytest`/`ruff` pass at every checkpoint.
- [x] No automatic like/unlike/delete/playlist mutation — none of the new modules call any mutating API.
- [x] Type hints + maintainable modules — every new file uses `from __future__ import annotations` and explicit type hints.
- [x] README updated — Task 11.

**Beyond spec (intentional, all explained inline):**

- 0.1 leftover cleanup (FK pragma, `NoReturn`, tz-aware `DateTime`, doctor source-scoping, source-string rename) folded into Task 1 because every later task touches at least one affected file.
- `youtube-likes` (CLI subcommand) vs `youtube_liked_videos` (DB source) naming intentional: short verbs at the CLI, descriptive snake_case in storage.
- Output buckets renamed to be direction-honest (`possibly_missing_from_ytmusic` = YT has it, YT Music doesn't; `ytmusic_only_likes` = YT Music has it, YT doesn't). The original spec listed these two as if they were distinct, but they describe the same direction; we kept them separate by direction with explicit names.
- Likes playlist id resolved via `channels.contentDetails.relatedPlaylists.likes` instead of hardcoding `"LL"` (which we keep as a fallback). Costs 1 extra quota unit per scan, far below the 10000/day default.

**Deliberately deferred:**

- **In-place 0.1 → 0.2 migration.** 0.2 is breaking; users delete their local DB and re-scan. Rationale: at MVP scale with effectively zero installed base, the cost of a robust migration exceeds the cost of "rm + rescan." README documents the upgrade step; Alembic / structured migrations land in 0.3+ once there's a real user base to protect.
- No automatic 0.1 → 0.2 version bump in `pyproject.toml` (a manual decision per release).
- Diagnosis status transitions (`closed`, `dismissed`, etc.) — the `status` column exists with default `open`; transitions land in 0.3 alongside the resolver.
- `compare-likes` only diffs the *latest* snapshot pair; arbitrary snapshot ids deferred (matches MVP 0.1's similar latest-snapshot scoping in doctor/scan).
- Test for `Config` paths — the new fields are exercised via the CLI smoke commands; no separate `tests/test_config.py` (consistent with 0.1's choice to skip it).
