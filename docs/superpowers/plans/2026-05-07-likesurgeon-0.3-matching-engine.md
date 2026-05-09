# likesurgeon 0.3 — Matching Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ghost detection (YouTube videos no longer playable) and drift detection (snapshot-pair metadata changes) to the existing cross-source matcher, surfaced through `compare-likes` / `issues` / `doctor` without any new CLI commands.

**Architecture:** Two-stage status pipeline (stage-1 in `fetch_video_statuses`, stage-2 disambiguation in scan command using `playlistItems.list` snippet titles), inline additive `ALTER TABLE` migration at engine construction, drift module operating on `SnapshotItem` pairs by `video_id`. Drift findings carry durable `SnapshotItem` ids in `reason` text — diagnostic only, not parseable.

**Tech Stack:** SQLAlchemy 2.0 (existing), `google-api-python-client` (existing — `videos.list`), RapidFuzz (existing — `token_sort_ratio`), pytest, ruff. No new deps.

**Branch:** create `feat/0.3-matching-engine` off `main` before Task 1.

**Spec:** [docs/superpowers/specs/2026-05-07-likesurgeon-0.3-matching-engine-design.md](../specs/2026-05-07-likesurgeon-0.3-matching-engine-design.md)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/likesurgeon/models.py` | Add `SnapshotItem.is_available` / `unavailable_reason` columns |
| `src/likesurgeon/db.py` | Add `_migrate_in_place(engine)` helper, wire into `make_engine` |
| `src/likesurgeon/youtube_client.py` | Add `VideoStatus` dataclass + `fetch_video_statuses` method (stage-1 mapping + batch failure policy) |
| `src/likesurgeon/snapshot.py` | Update `_youtube_to_record` to read `_likesurgeon_video_status` and strip namespace from `rec["raw"]`; extend `create_snapshot` to copy `rec["is_available"]` / `rec["unavailable_reason"]`; add `latest_snapshots_for_source(session, source, *, limit)` helper |
| `src/likesurgeon/cli.py` | Wire stage-2 disambiguation + injection into `scan_youtube_likes`; add `_PipelineResult` + `_compare_and_persist` orchestrator (ghost + drift wiring lives here, NOT in `compare.py`); extend `compare-likes` summary table + `doctor` print line + `issues --type` help text |
| `src/likesurgeon/drift.py` | NEW: `DriftFinding` dataclass + `detect_drift` pure function |
| `src/likesurgeon/diagnosis.py` | Add `ISSUE_UNAVAILABLE_VIDEO` / `ISSUE_METADATA_DRIFT` constants + `build_unavailable_video_items` / `build_metadata_drift_items` helpers (return `DiagnosisItem` rows; orchestration that calls them lives in `cli.py`) |
| `src/likesurgeon/doctor.py` | Add `unavailable_videos` / `metadata_drift` count fields to `DiagnosisSummary` + extend `_latest_diagnosis_summary` aggregation. (User-facing render lives in `cli.py` `doctor()`, modified separately in Task 9.) |
| `tests/test_db.py` | NEW: `_migrate_in_place` tests |
| `tests/test_youtube_client.py` | `fetch_video_statuses` tests |
| `tests/test_snapshot.py` | Translator + `create_snapshot` extension tests; `latest_snapshots_for_source` tests |
| `tests/test_drift.py` | NEW: `detect_drift` tests |
| `tests/test_compare.py` | Ghost + drift integration tests |
| `tests/test_doctor.py` | Doctor count tests |
| `README.md` | Roadmap row, "What it does today" feature line, compare-likes example, delete "Upgrading from 0.1" |

CLI Typer-runner tests are skipped per spec, but the stage-2 + injection pipeline inside `scan_youtube_likes` is extracted as a pure helper (`attach_video_statuses`, Task 5) and unit-tested. Only the thin Typer wrapper around it relies on the manual e2e at the end.

---

## Task 1: Schema columns + inline migration

**Files:**
- Modify: `src/likesurgeon/models.py:90-105` (add two columns to `SnapshotItem`)
- Modify: `src/likesurgeon/db.py` (add `_migrate_in_place`, wire into `make_engine`, update `init_db` docstring)
- Create: `tests/test_db.py`

- [ ] **Step 1: Branch off main**

```bash
git checkout main && git pull && git checkout -b feat/0.3-matching-engine
```

- [ ] **Step 2: Write the failing migration tests**

Create `tests/test_db.py`:

```python
"""Tests for the inline schema migration helper."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from likesurgeon.db import _migrate_in_place, init_db, make_engine


def test_migrate_in_place_no_op_on_fresh_install(tmp_path: Path) -> None:
    """Fresh install: no `snapshot_items` table yet → migration is a no-op."""
    engine = make_engine(tmp_path / "fresh.sqlite")
    _migrate_in_place(engine)  # must not raise

    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
    assert "snapshot_items" not in tables


def test_migrate_in_place_adds_columns_to_pre_0_3_db(tmp_path: Path) -> None:
    """Simulate a 0.2-shape DB (no is_available / unavailable_reason) and
    verify the migration adds both columns idempotently."""
    db_path = tmp_path / "legacy.sqlite"
    legacy = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE snapshot_items (id INTEGER PRIMARY KEY, title TEXT)"
        ))

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
    init_db(engine)            # creates snapshot_items with columns already present
    _migrate_in_place(engine)  # first migration call — no-op for the new columns
    _migrate_in_place(engine)  # second call — must also be a no-op, not error


def test_make_engine_runs_migration(tmp_path: Path) -> None:
    """`make_engine` itself wires `_migrate_in_place` so every command path
    transparently picks up the schema. Simulate a 0.2 DB and verify the
    columns appear after `make_engine` alone (no separate migration call)."""
    db_path = tmp_path / "auto.sqlite"
    legacy = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy.begin() as conn:
        conn.execute(text(
            "CREATE TABLE snapshot_items (id INTEGER PRIMARY KEY, title TEXT)"
        ))

    make_engine(db_path)  # should run the migration as a side effect

    verify = create_engine(f"sqlite:///{db_path}", future=True)
    with verify.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(snapshot_items)"))}
    assert "is_available" in cols
    assert "unavailable_reason" in cols
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/test_db.py -v
```

Expected: FAIL with `ImportError: cannot import name '_migrate_in_place'`.

- [ ] **Step 4: Add the new columns to `SnapshotItem`**

Edit `src/likesurgeon/models.py`. After the existing `music_candidate_reason` column (around line 105), before `snapshot: Mapped[Snapshot] = relationship(...)`, insert:

```python
    # Ghost detection (0.3): availability of the underlying YouTube video at
    # scan time. ``None`` means "unknown" (snapshot taken before 0.3, or
    # status check failed). Only populated for ``youtube_liked_videos`` rows.
    is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

Add `Boolean` to the existing SQLAlchemy import at the top of the file if not already present.

- [ ] **Step 5: Implement `_migrate_in_place` and wire it into `make_engine`**

Replace `src/likesurgeon/db.py` body (after the existing `_sqlite_fk_pragma`):

```python
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
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "snapshot_items" not in tables:
            return
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(snapshot_items)")
        }
        for name, decl in (
            ("is_available", "BOOLEAN"),
            ("unavailable_reason", "VARCHAR(32)"),
        ):
            if name not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE snapshot_items ADD COLUMN {name} {decl}"
                )


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
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_db.py -v
```

Expected: 4 PASS.

- [ ] **Step 7: Run the full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green. If ruff format check fails, run `uv run ruff format .` and re-run.

- [ ] **Step 8: Commit**

```bash
git add src/likesurgeon/models.py src/likesurgeon/db.py tests/test_db.py
git commit -m "feat(schema): add SnapshotItem.is_available/unavailable_reason + inline migration"
```

---

## Task 2: VideoStatus + fetch_video_statuses (stage-1 mapping)

**Files:**
- Modify: `src/likesurgeon/youtube_client.py` (add `VideoStatus` dataclass, `fetch_video_statuses` method)
- Modify: `tests/test_youtube_client.py` (append tests)

- [ ] **Step 1: Write failing tests for stage-1 mapping**

Append to `tests/test_youtube_client.py` (preserve all existing imports and tests):

```python
def test_video_status_dataclass_field_types() -> None:
    """VideoStatus mirrors SnapshotItem's tri-state: bool | None for
    `is_available` (available / unavailable / unknown) and str | None
    for `reason`."""
    from likesurgeon.youtube_client import VideoStatus

    available = VideoStatus(is_available=True, reason=None)
    unavailable = VideoStatus(is_available=False, reason="deleted")
    unknown = VideoStatus(is_available=None, reason="status_check_failed")
    assert available.is_available is True
    assert unavailable.is_available is False
    assert unknown.is_available is None


def test_fetch_video_statuses_maps_uploadStatus_rejected(monkeypatch) -> None:
    """`status.uploadStatus = "rejected"` → is_available=False, reason='rejected'."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake_videos_list(*, ids: list[str]) -> dict:
        return {"items": [
            {"id": "rej1", "status": {"uploadStatus": "rejected", "privacyStatus": "public"}},
        ]}

    monkeypatch.setattr(client, "_videos_list", fake_videos_list)
    out = client.fetch_video_statuses(["rej1"])
    assert out["rej1"] == VideoStatus(is_available=False, reason="rejected")


def test_fetch_video_statuses_maps_uploadStatus_deleted(monkeypatch) -> None:
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(client, "_videos_list", lambda *, ids: {"items": [
        {"id": "del1", "status": {"uploadStatus": "deleted", "privacyStatus": "public"}},
    ]})

    out = client.fetch_video_statuses(["del1"])
    assert out["del1"] == VideoStatus(is_available=False, reason="deleted")


def test_fetch_video_statuses_maps_present_video_to_available(monkeypatch) -> None:
    """Anything not rejected/deleted, when the video resource is present,
    is treated as available — including private videos returned to the
    owner (they can still play it). See spec for false-ghost discussion."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(client, "_videos_list", lambda *, ids: {"items": [
        {"id": "pub", "status": {"uploadStatus": "processed", "privacyStatus": "public"}},
        {"id": "unl", "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"}},
        {"id": "own", "status": {"uploadStatus": "processed", "privacyStatus": "private"}},
    ]})

    out = client.fetch_video_statuses(["pub", "unl", "own"])
    assert out["pub"] == VideoStatus(is_available=True, reason=None)
    assert out["unl"] == VideoStatus(is_available=True, reason=None)
    assert out["own"] == VideoStatus(is_available=True, reason=None)


def test_fetch_video_statuses_emits_placeholder_for_missing(monkeypatch) -> None:
    """IDs absent from the response → placeholder VideoStatus that the
    scan command will disambiguate via snippet titles. fetch_video_statuses
    NEVER emits 'private'/'deleted'/'unavailable' for missing IDs — those
    decisions belong to stage 2."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    monkeypatch.setattr(client, "_videos_list", lambda *, ids: {"items": [
        {"id": "present", "status": {"uploadStatus": "processed", "privacyStatus": "public"}},
    ]})

    out = client.fetch_video_statuses(["present", "gone"])
    assert out["present"] == VideoStatus(is_available=True, reason=None)
    assert out["gone"] == VideoStatus(is_available=False, reason="missing_from_videos_list")


def test_fetch_video_statuses_batches_by_50(monkeypatch) -> None:
    """120 IDs should produce 3 calls (50 + 50 + 20). The function returns
    one entry per input ID regardless of batching."""
    from likesurgeon.youtube_client import YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    calls: list[list[str]] = []

    def fake(*, ids: list[str]) -> dict:
        calls.append(list(ids))
        return {"items": [
            {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
            for vid in ids
        ]}

    monkeypatch.setattr(client, "_videos_list", fake)
    video_ids = [f"v{i}" for i in range(120)]
    out = client.fetch_video_statuses(video_ids)

    assert len(calls) == 3
    assert [len(c) for c in calls] == [50, 50, 20]
    assert set(out.keys()) == set(video_ids)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_youtube_client.py -v -k "video_status or fetch_video_statuses"
```

Expected: FAIL with `ImportError: cannot import name 'VideoStatus'`.

- [ ] **Step 3: Implement `VideoStatus` and `fetch_video_statuses`**

In `src/likesurgeon/youtube_client.py`, add a `from dataclasses import dataclass` import if missing, then after the existing exception classes (around line 40) append:

```python
@dataclass(frozen=True)
class VideoStatus:
    """Result of a single video's availability check.

    ``is_available`` is tri-state to mirror ``SnapshotItem.is_available``:
    ``True`` (playable), ``False`` (unavailable), ``None`` (unknown — usually
    a status-check failure).
    """

    is_available: bool | None
    reason: str | None
```

Inside the `YouTubeClient` class (after `fetch_liked_videos`), append:

```python
    _STATUS_BATCH_SIZE = 50

    def _videos_list(self, *, ids: list[str]) -> dict[str, Any]:
        """Thin wrapper around `videos.list?part=status&id=<ids>`.

        Split out so tests can monkeypatch this single seam without faking
        the entire `googleapiclient` discovery surface. Production callers
        go through `fetch_video_statuses`.

        Note: `maxResults` is intentionally NOT passed. Per the official
        videos.list documentation, `maxResults` is only valid when filtering
        by `chart` or `myRating`; with the `id` filter, supplying it makes
        the live request fail. The API returns one item per requested ID
        anyway.
        """
        service = self._service()
        return (
            service.videos()
            .list(part="status", id=",".join(ids))
            .execute()
        )

    def fetch_video_statuses(self, video_ids: list[str]) -> dict[str, VideoStatus]:
        """Stage-1 status check via batched `videos.list?part=status`.

        Returns one entry per input ID. IDs missing from the API response
        get the placeholder ``VideoStatus(is_available=False,
        reason='missing_from_videos_list')`` — this function takes only
        video IDs and has no access to ``playlistItems.list`` snippet
        titles, so it cannot distinguish "made private" from "deleted"
        from "region-restricted". The scan command resolves the
        placeholder into a final reason. The placeholder must never reach
        the database.

        Batch failure handling lands in Task 3.
        """
        out: dict[str, VideoStatus] = {}
        for start in range(0, len(video_ids), self._STATUS_BATCH_SIZE):
            chunk = video_ids[start : start + self._STATUS_BATCH_SIZE]
            resp = self._videos_list(ids=chunk)
            seen: set[str] = set()
            for item in resp.get("items", []):
                vid = item["id"]
                seen.add(vid)
                out[vid] = _classify_status(item.get("status") or {})
            for vid in chunk:
                if vid not in seen:
                    out[vid] = VideoStatus(
                        is_available=False, reason="missing_from_videos_list"
                    )
        return out
```

Above the `YouTubeClient` class (or below `VideoStatus`), add the pure helper:

```python
def _classify_status(status: dict[str, Any]) -> VideoStatus:
    """Stage-1 status mapping for a present video resource. See spec
    "Status mapping pipeline" for the full rules. privacyStatus is
    deliberately NOT consulted: a returned private video means the caller
    is the owner (or has explicit access), so it's playable."""
    upload = status.get("uploadStatus")
    if upload == "rejected":
        return VideoStatus(is_available=False, reason="rejected")
    if upload == "deleted":
        return VideoStatus(is_available=False, reason="deleted")
    return VideoStatus(is_available=True, reason=None)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_youtube_client.py -v -k "video_status or fetch_video_statuses"
```

Expected: 6 PASS.

- [ ] **Step 5: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/youtube_client.py tests/test_youtube_client.py
git commit -m "feat(youtube): add VideoStatus + fetch_video_statuses (stage-1 status mapping)"
```

---

## Task 3: Batch failure policy for `fetch_video_statuses`

**Files:**
- Modify: `src/likesurgeon/youtube_client.py` (wrap `_videos_list` calls with retry/quota/exception handling)
- Modify: `tests/test_youtube_client.py` (append tests)

- [ ] **Step 1: Write failing tests for batch failure policy**

Append to `tests/test_youtube_client.py`:

```python
def test_fetch_video_statuses_retries_on_5xx(monkeypatch) -> None:
    """5xx → retry. Two failures then success → all IDs return real statuses,
    no `status_check_failed`."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    attempts = {"n": 0}

    def fake(*, ids: list[str]) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            from googleapiclient.errors import HttpError

            raise HttpError(_FakeResp(503), b"server")
        return {"items": [
            {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
            for vid in ids
        ]}

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))  # zero-delay test

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert attempts["n"] == 3


def test_fetch_video_statuses_marks_batch_failed_after_retry_exhaustion(
    monkeypatch,
) -> None:
    """Persistent 5xx → batch items get status_check_failed, scan continues."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def always_5xx(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError
        raise HttpError(_FakeResp(503), b"down")

    monkeypatch.setattr(client, "_videos_list", always_5xx)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["b"] == VideoStatus(is_available=None, reason="status_check_failed")


def test_fetch_video_statuses_quota_exceeded_short_circuits_remaining(
    monkeypatch,
) -> None:
    """403 quotaExceeded → bail entire status-check phase. First batch's
    items are real, remaining batches' items get status_check_failed."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    seen: list[list[str]] = []

    # Realistic Google API quota body — matches what `videos.list` returns
    # when the daily quota is exhausted. The HTTP `reason` header is just
    # "Forbidden"; the `quotaExceeded` reason lives inside the JSON body.
    quota_body = (
        b'{"error":{"code":403,'
        b'"errors":[{"reason":"quotaExceeded","domain":"youtube.quota"}]}}'
    )

    def fake(*, ids: list[str]) -> dict:
        seen.append(list(ids))
        if len(seen) == 1:
            return {"items": [
                {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
                for vid in ids
            ]}
        from googleapiclient.errors import HttpError
        raise HttpError(_FakeResp(403), quota_body)

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 2)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b", "c", "d"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert out["c"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["d"] == VideoStatus(is_available=None, reason="status_check_failed")
    # Only the first failing batch is attempted (no retries on quota).
    assert len(seen) == 2


def test_fetch_video_statuses_retries_on_transport_error(monkeypatch) -> None:
    """Per spec 'Network error / HTTP 5xx': non-HttpError transport
    failures (TimeoutError, ConnectionResetError, etc.) retry just like
    5xx. Two failures then success → all IDs return real statuses, no
    `status_check_failed`."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)
    attempts = {"n": 0}

    def fake(*, ids: list[str]) -> dict:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("network down")
        return {"items": [
            {"id": vid, "status": {"uploadStatus": "processed", "privacyStatus": "public"}}
            for vid in ids
        ]}

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=True, reason=None)
    assert out["b"] == VideoStatus(is_available=True, reason=None)
    assert attempts["n"] == 3


def test_fetch_video_statuses_403_with_unexpected_body_falls_through_to_failed(
    monkeypatch,
) -> None:
    """403 whose body isn't the documented `{"error": {"errors": [...]}}`
    shape (e.g. plain string body, JSON with `error` as a non-dict, errors
    list of non-dicts) must NOT crash _is_quota_exceeded — it falls through
    to the generic `status_check_failed` path so one weird response can't
    abort the whole scan."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError
        # Malformed: `error` is a string, not the expected dict.
        raise HttpError(_FakeResp(403), b'{"error": "Forbidden"}')

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b"])
    assert out["a"] == VideoStatus(is_available=None, reason="status_check_failed")
    assert out["b"] == VideoStatus(is_available=None, reason="status_check_failed")


def test_fetch_video_statuses_404_batch_size_one_treats_as_deleted(
    monkeypatch,
) -> None:
    """404 videoNotFound on a 1-ID batch → that ID is deleted."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError
        raise HttpError(_FakeResp(404), b"not found")

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 1)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["bogus"])
    assert out["bogus"] == VideoStatus(is_available=False, reason="deleted")


def test_fetch_video_statuses_404_multi_id_batch_marks_all_failed(
    monkeypatch,
) -> None:
    """404 on a >1-ID batch is undocumented but defensive — mark whole
    batch status_check_failed without binary-splitting."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        from googleapiclient.errors import HttpError
        raise HttpError(_FakeResp(404), b"weird")

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_STATUS_BATCH_SIZE", 5)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(["a", "b", "c"])
    for v in ("a", "b", "c"):
        assert out[v] == VideoStatus(is_available=None, reason="status_check_failed")


# Tiny test helper: mimics googleapiclient.errors.HttpError's resp object.
# Only `.status` is read by the production retry/quota logic — the actual
# error reason now lives in the HTTP body bytes (see _is_quota_exceeded).
class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "fail"  # set so str(HttpError) doesn't blow up if printed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_youtube_client.py -v -k "fetch_video_statuses_retries or fetch_video_statuses_marks or fetch_video_statuses_quota or fetch_video_statuses_404"
```

Expected: FAIL — current `fetch_video_statuses` doesn't catch HttpError; first attempt raises and the test hits an unwrapped exception.

- [ ] **Step 3: Add retry/quota/exception handling**

In `src/likesurgeon/youtube_client.py`, replace the `fetch_video_statuses` method body. First add a `_RETRY_SLEEPS` class attribute (just below `_STATUS_BATCH_SIZE`):

```python
    _RETRY_SLEEPS: tuple[float, ...] = (1.0, 3.0)  # delays before retry 1 and 2
```

Then add a module-level helper for quota detection (above the `YouTubeClient` class — or below it, anywhere at module scope). The HTTP "reason" string for a 403 from Google is just `"Forbidden"`; the actual cause (`quotaExceeded` / `dailyLimitExceeded` / `rateLimitExceeded`) lives inside the JSON error body. Parse it explicitly so we don't mis-bucket real quota exhaustion as a generic failure (and waste retries on it):

```python
def _is_quota_exceeded(exc: Any) -> bool:
    """Detect Google API quota exhaustion from the JSON error body.

    The HTTP-level `e.resp.reason` is just "Forbidden" for any 403, so we
    parse `e.content` (bytes) instead — the body's `error.errors[].reason`
    field is what carries `quotaExceeded` / `dailyLimitExceeded` /
    `rateLimitExceeded`.

    Defensive at every shape boundary: non-403 responses, missing bodies,
    non-UTF-8 bytes, malformed JSON, or any payload whose shape doesn't
    precisely match `{"error": {"errors": [{"reason": ...}, ...]}}` all
    return False rather than raising. We'd rather mis-classify an
    edge-case 403 as a non-quota failure (and let it fall through the
    retry/`failed` path) than abort the entire scan because Google
    returned a body we didn't predict.
    """
    import json

    if getattr(getattr(exc, "resp", None), "status", None) != 403:
        return False
    content = getattr(exc, "content", None)
    if not content:
        return False
    try:
        payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    if not isinstance(err, dict):
        return False
    errors = err.get("errors")
    if not isinstance(errors, list):
        return False
    quota_reasons = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
    return any(
        isinstance(e, dict) and e.get("reason") in quota_reasons for e in errors
    )
```

(`Any` is already imported at the top of `youtube_client.py`. The `import json` stays inside the function so the JSON parser isn't loaded for the hot path that doesn't see quota errors.)

Then rewrite `fetch_video_statuses`:

```python
    def _videos_list_with_retry(self, *, ids: list[str]) -> dict[str, Any] | str:
        """Call `_videos_list` with retry on transport errors AND HTTP 5xx
        (the spec's "Network error / HTTP 5xx" bucket). Returns the
        response dict on success, or a string sentinel on failure:
        ``"quota"`` / ``"404"`` / ``"failed"``. The caller maps each
        sentinel to the right policy.

        Quota and 404 short-circuit (no retry — same condition would
        repeat). 5xx and arbitrary non-HttpError exceptions (timeouts,
        socket resets, urllib3 connection errors) are retried; the retry
        budget is bounded by ``_RETRY_SLEEPS``, so a true programming bug
        that raises a non-HTTP exception will eventually fall through to
        ``"failed"`` rather than loop forever.
        """
        import time

        from googleapiclient.errors import HttpError

        attempts: list[float] = [0.0, *self._RETRY_SLEEPS]
        last_exc: Exception | None = None
        for delay in attempts:
            if delay:
                time.sleep(delay)
            try:
                return self._videos_list(ids=ids)
            except HttpError as e:
                status = getattr(e.resp, "status", None)
                if _is_quota_exceeded(e):
                    return "quota"
                if status == 404:
                    return "404"
                if status is not None and 500 <= status < 600:
                    last_exc = e
                    continue
                # Non-retryable HTTP error (e.g. 401 auth, 400 bad request).
                last_exc = e
                break
            except Exception as e:  # noqa: BLE001 — system-boundary catch
                # Transport-level error (timeout, socket reset, DNS failure,
                # etc.). Per spec, retry just like 5xx — bounded by the
                # `_RETRY_SLEEPS` budget so it can't spin indefinitely.
                last_exc = e
                continue
        # Exhausted retries or non-retryable error.
        if last_exc is not None and isinstance(last_exc, HttpError):
            status = getattr(last_exc.resp, "status", None)
            if status == 404:
                return "404"
        return "failed"

    def fetch_video_statuses(self, video_ids: list[str]) -> dict[str, VideoStatus]:
        """Stage-1 status check via batched `videos.list?part=status`.

        Returns one entry per input ID. See spec "Status mapping pipeline"
        and "Batch failure policy" for the full semantics.
        """
        out: dict[str, VideoStatus] = {}
        bail_remaining = False
        for start in range(0, len(video_ids), self._STATUS_BATCH_SIZE):
            chunk = video_ids[start : start + self._STATUS_BATCH_SIZE]
            if bail_remaining:
                for vid in chunk:
                    out[vid] = VideoStatus(
                        is_available=None, reason="status_check_failed"
                    )
                continue

            resp = self._videos_list_with_retry(ids=chunk)
            if resp == "quota":
                bail_remaining = True
                for vid in chunk:
                    out[vid] = VideoStatus(
                        is_available=None, reason="status_check_failed"
                    )
                continue
            if resp == "404":
                if len(chunk) == 1:
                    out[chunk[0]] = VideoStatus(is_available=False, reason="deleted")
                else:
                    for vid in chunk:
                        out[vid] = VideoStatus(
                            is_available=None, reason="status_check_failed"
                        )
                continue
            if resp == "failed":
                for vid in chunk:
                    out[vid] = VideoStatus(
                        is_available=None, reason="status_check_failed"
                    )
                continue
            # Success — `resp` is the dict.
            assert isinstance(resp, dict)
            seen: set[str] = set()
            for item in resp.get("items", []):
                vid = item["id"]
                seen.add(vid)
                out[vid] = _classify_status(item.get("status") or {})
            for vid in chunk:
                if vid not in seen:
                    out[vid] = VideoStatus(
                        is_available=False, reason="missing_from_videos_list"
                    )
        return out
```

- [ ] **Step 4: Run failure tests to verify they pass**

```bash
uv run pytest tests/test_youtube_client.py -v -k "fetch_video_statuses"
```

Expected: 12 PASS — 4 stage-1 mapping (`…_maps_uploadStatus_rejected`, `…_maps_uploadStatus_deleted`, `…_maps_present_video_to_available`, `…_emits_placeholder_for_missing`) + 1 batching (`…_batches_by_50`) + 7 failure modes (`…_retries_on_5xx`, `…_marks_batch_failed_after_retry_exhaustion`, `…_quota_exceeded_short_circuits_remaining`, `…_retries_on_transport_error`, `…_403_with_unexpected_body_falls_through_to_failed`, `…_404_batch_size_one_treats_as_deleted`, `…_404_multi_id_batch_marks_all_failed`). The `-k "fetch_video_statuses"` filter intentionally excludes `test_video_status_dataclass_field_types` (its name doesn't contain `fetch_video_statuses`).

- [ ] **Step 5: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/youtube_client.py tests/test_youtube_client.py
git commit -m "feat(youtube): batch failure policy for fetch_video_statuses (5xx retry, quota bail, 404)"
```

---

## Task 4: Translator + create_snapshot extension

**Files:**
- Modify: `src/likesurgeon/snapshot.py:95-139` (`_youtube_to_record`) and `:186-220` (`create_snapshot`)
- Modify: `tests/test_snapshot.py` (append tests)

- [ ] **Step 1: Write failing tests for translator + create_snapshot extension**

Append to `tests/test_snapshot.py`:

```python
def test_youtube_translator_reads_likesurgeon_video_status() -> None:
    """Raw item with `_likesurgeon_video_status` augmentation → rec carries
    `is_available` / `unavailable_reason`."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {
            "title": "Song",
            "channelTitle": "Artist",
            "resourceId": {"videoId": "vid1"},
        },
        "contentDetails": {"videoId": "vid1"},
        "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
    }
    rec = _youtube_to_record(raw)
    assert rec["is_available"] is False
    assert rec["unavailable_reason"] == "deleted"


def test_youtube_translator_strips_likesurgeon_namespace_from_raw() -> None:
    """rec["raw"] must NOT contain any `_likesurgeon_*` key — those are
    our internal augmentation and would (a) leak into raw_json and (b)
    crash json.dumps if they were dataclass instances."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {
            "title": "T",
            "channelTitle": "C",
            "resourceId": {"videoId": "vid"},
        },
        "contentDetails": {"videoId": "vid"},
        "_likesurgeon_video_status": {"is_available": True, "reason": None},
    }
    rec = _youtube_to_record(raw)
    assert "_likesurgeon_video_status" not in rec["raw"]
    # Genuine API fields survive.
    assert "snippet" in rec["raw"]


def test_youtube_translator_handles_missing_augmentation() -> None:
    """No injection → rec[is_available] / rec[unavailable_reason] default
    to None. Backward compat with tests/fixtures that don't set the key."""
    from likesurgeon.snapshot import _youtube_to_record

    raw = {
        "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }
    rec = _youtube_to_record(raw)
    assert rec["is_available"] is None
    assert rec["unavailable_reason"] is None


def test_create_snapshot_persists_is_available_columns(session) -> None:
    """End-to-end: scan-shaped raw item with augmentation → SnapshotItem
    rows have is_available / unavailable_reason set, raw_json is clean."""
    import json as json_lib

    from likesurgeon.snapshot import create_snapshot, get_snapshot_items

    items = [{
        "snippet": {
            "title": "Gone",
            "channelTitle": "Owner",
            "resourceId": {"videoId": "vGone"},
        },
        "contentDetails": {"videoId": "vGone"},
        "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
    }]
    snap = create_snapshot(session, "youtube_liked_videos", items)
    rows = get_snapshot_items(session, snap.id)

    assert len(rows) == 1
    assert rows[0].is_available is False
    assert rows[0].unavailable_reason == "deleted"
    # raw_json must not carry the injection.
    assert "_likesurgeon_video_status" not in json_lib.loads(rows[0].raw_json)


def test_create_snapshot_ytmusic_translator_leaves_columns_null(session) -> None:
    """YT Music translator does not set is_available; the columns stay NULL
    even if (somehow) the injection field is present on a YT Music item."""
    from likesurgeon.snapshot import create_snapshot, get_snapshot_items

    items = [{
        "videoId": "ytm1",
        "title": "Track",
        "artists": [{"name": "Artist"}],
        "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
    }]
    snap = create_snapshot(session, "ytmusic_liked_songs", items)
    rows = get_snapshot_items(session, snap.id)
    assert rows[0].is_available is None
    assert rows[0].unavailable_reason is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_snapshot.py -v -k "translator or is_available_columns"
```

Expected: FAIL — current translator doesn't read augmentation, current `create_snapshot` doesn't pass new fields.

- [ ] **Step 3: Update `_youtube_to_record` to read injection + strip namespace**

In `src/likesurgeon/snapshot.py`, replace the `return { ... }` block at the end of `_youtube_to_record` (lines ~126-139) with:

```python
    augmentation = item.get("_likesurgeon_video_status") or {}
    raw = {k: v for k, v in item.items() if not k.startswith("_likesurgeon_")}

    return {
        "video_id": video_id,
        "title": title,
        "artists": artists,
        "album": None,
        "duration_seconds": None,
        "thumbnails": (snippet.get("thumbnails") or None),
        "canonical_key": canon,
        "dedupe_key": dedupe,
        "raw": raw,
        "is_music_candidate": classification.is_music_candidate,
        "music_candidate_score": classification.score,
        "music_candidate_reason": classification.reason,
        "is_available": augmentation.get("is_available"),
        "unavailable_reason": augmentation.get("reason"),
    }
```

- [ ] **Step 4: Update `_ytmusic_to_record` to set the new keys to None**

In the same file, find `_ytmusic_to_record` (around line 64) and add the two new keys to its returned dict (the columns stay NULL for YT Music rows by design):

```python
        "is_available": None,
        "unavailable_reason": None,
```

- [ ] **Step 5: Update `create_snapshot` to copy the new rec fields**

In `create_snapshot` (lines ~200-217), inside the `SnapshotItem(...)` constructor call, append two more keyword arguments:

```python
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
                    json.dumps(rec["thumbnails"], ensure_ascii=False) if rec["thumbnails"] else None
                ),
                canonical_key=rec["canonical_key"],
                raw_json=json.dumps(rec["raw"], ensure_ascii=False),
                is_music_candidate=rec["is_music_candidate"],
                music_candidate_score=rec["music_candidate_score"],
                music_candidate_reason=rec["music_candidate_reason"],
                is_available=rec.get("is_available"),
                unavailable_reason=rec.get("unavailable_reason"),
            )
```

`.get(...)` defaults to `None` so any code path that constructs a rec without the new keys (e.g. tests, future translators) stays compatible.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_snapshot.py -v -k "translator or is_available_columns"
```

Expected: 5 PASS.

- [ ] **Step 7: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/likesurgeon/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): wire is_available/unavailable_reason through translator and create_snapshot"
```

---

## Task 5: scan_youtube_likes integration (stage-2 + injection)

**Files:**
- Modify: `src/likesurgeon/youtube_client.py` (add stage-2 disambiguation helper)
- Modify: `src/likesurgeon/cli.py:202-232` (`scan_youtube_likes`)
- Modify: `tests/test_youtube_client.py` (append tests for the disambiguator)

- [ ] **Step 1: Write failing tests for the stage-2 disambiguator**

Append to `tests/test_youtube_client.py`:

```python
def test_disambiguate_status_via_snippet_title_private() -> None:
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="Private video")
    assert out == VideoStatus(is_available=False, reason="private")


def test_disambiguate_status_via_snippet_title_deleted() -> None:
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="Deleted video")
    assert out == VideoStatus(is_available=False, reason="deleted")


def test_disambiguate_status_falls_through_to_unavailable() -> None:
    """Region-restricted, age-gated, weird API state → conservative
    'unavailable' rather than misclassifying."""
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    placeholder = VideoStatus(is_available=False, reason="missing_from_videos_list")
    out = disambiguate_video_status(placeholder, snippet_title="K-pop hit (region-locked)")
    assert out == VideoStatus(is_available=False, reason="unavailable")


def test_disambiguate_status_passes_through_non_placeholder() -> None:
    """Non-placeholder VideoStatus is returned unchanged — disambiguation
    only applies to the missing-from-response case."""
    from likesurgeon.youtube_client import VideoStatus, disambiguate_video_status

    available = VideoStatus(is_available=True, reason=None)
    rejected = VideoStatus(is_available=False, reason="rejected")
    failed = VideoStatus(is_available=None, reason="status_check_failed")
    assert disambiguate_video_status(available, snippet_title="anything") == available
    assert disambiguate_video_status(rejected, snippet_title="Private video") == rejected
    assert disambiguate_video_status(failed, snippet_title="Deleted video") == failed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_youtube_client.py -v -k "disambiguate_status"
```

Expected: FAIL — `disambiguate_video_status` not exported.

- [ ] **Step 3: Implement `disambiguate_video_status`**

In `src/likesurgeon/youtube_client.py`, after `_classify_status`:

```python
def disambiguate_video_status(
    status: VideoStatus, *, snippet_title: str
) -> VideoStatus:
    """Stage-2 disambiguator for `missing_from_videos_list` placeholders.

    Non-placeholder statuses pass through unchanged. The placeholder is
    rewritten using the `playlistItems.list` snippet title YouTube returns
    when the caller can't access a referenced video — `"Private video"` and
    `"Deleted video"` are documented placeholders; anything else falls
    through to the conservative `"unavailable"` reason.
    """
    if status.reason != "missing_from_videos_list":
        return status
    title = (snippet_title or "").strip()
    if title == "Private video":
        return VideoStatus(is_available=False, reason="private")
    if title == "Deleted video":
        return VideoStatus(is_available=False, reason="deleted")
    return VideoStatus(is_available=False, reason="unavailable")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_youtube_client.py -v -k "disambiguate_status"
```

Expected: 4 PASS.

- [ ] **Step 5: Add a testable `attach_video_statuses` helper**

Stage-2 + injection is more than 5 lines and has branching (skip items without `video_id`, look up titles, mutate the dict). Extract it as a pure helper so it's unit-testable without faking the entire Typer/CLI surface. Append to `src/likesurgeon/youtube_client.py` (next to `disambiguate_video_status`):

```python
def attach_video_statuses(
    items: list[dict[str, Any]], statuses: dict[str, VideoStatus]
) -> None:
    """Mutate raw `playlistItems.list` entries in place: stage-2 disambiguate
    `missing_from_videos_list` placeholders against snippet titles, then
    inject the final status onto each item as `_likesurgeon_video_status`
    *primitive dict* (NOT the VideoStatus dataclass — see spec
    "Persistence" for why; the snapshot ingestion runs `json.dumps` on
    `rec["raw"]` and would crash on a dataclass instance).

    Items missing `video_id` are skipped (rare YT data quirk; nothing to
    look up in `statuses` for them). The function does not return; it
    mutates `items` so they can be passed straight to `create_snapshot`.
    """
    for it in items:
        snippet = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        vid = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if not vid:
            continue
        title = str(snippet.get("title") or "")
        final = disambiguate_video_status(statuses[vid], snippet_title=title)
        it["_likesurgeon_video_status"] = {
            "is_available": final.is_available,
            "reason": final.reason,
        }
```

- [ ] **Step 6: Write a failing test for `attach_video_statuses`**

Append to `tests/test_youtube_client.py`:

```python
def test_attach_video_statuses_injects_disambiguated_dict() -> None:
    """End-to-end of stage-2 + injection: placeholder + 'Private video'
    title → injected dict says private; available status passes through."""
    from likesurgeon.youtube_client import VideoStatus, attach_video_statuses

    items = [
        {
            "snippet": {"title": "Private video", "resourceId": {"videoId": "p1"}},
            "contentDetails": {"videoId": "p1"},
        },
        {
            "snippet": {"title": "Real Title", "resourceId": {"videoId": "ok"}},
            "contentDetails": {"videoId": "ok"},
        },
        {
            "snippet": {"title": "no id"},  # missing video_id — skipped
            "contentDetails": {},
        },
    ]
    statuses = {
        "p1": VideoStatus(is_available=False, reason="missing_from_videos_list"),
        "ok": VideoStatus(is_available=True, reason=None),
    }
    attach_video_statuses(items, statuses)

    # Injected as PRIMITIVE DICT (not VideoStatus dataclass) so json.dumps
    # over rec["raw"] in snapshot ingestion can't crash.
    assert items[0]["_likesurgeon_video_status"] == {
        "is_available": False,
        "reason": "private",
    }
    assert items[1]["_likesurgeon_video_status"] == {
        "is_available": True,
        "reason": None,
    }
    # Item without a video_id had nothing to inject.
    assert "_likesurgeon_video_status" not in items[2]
```

- [ ] **Step 7: Run the helper test to verify it passes**

```bash
uv run pytest tests/test_youtube_client.py -v -k "attach_video_statuses"
```

Expected: PASS.

- [ ] **Step 8: Wire stages 1+2 + injection into `scan_youtube_likes`**

Replace the body of `scan_youtube_likes` in `src/likesurgeon/cli.py` (lines ~202-232):

```python
@app.command()
def scan_youtube_likes(
    limit: Annotated[int, typer.Option(help="Maximum number of liked videos to fetch.")] = 5000,
) -> None:
    """Fetch YouTube liked videos (LL playlist) and store a snapshot.

    Also runs a per-video availability check (`videos.list?part=status`) and
    persists the result as `SnapshotItem.is_available` / `unavailable_reason`
    so `compare-likes` can surface ghost videos. See spec "Status mapping
    pipeline".
    """
    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
        attach_video_statuses,
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

    # Stage 1 + 2 + injection. attach_video_statuses mutates items in place
    # so they can be passed straight to create_snapshot below.
    video_ids: list[str] = []
    for it in items:
        snippet = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        vid = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if vid:
            video_ids.append(vid)

    statuses = client.fetch_video_statuses(video_ids)
    attach_video_statuses(items, statuses)

    with session_scope(factory) as session:
        snap = create_snapshot(session, "youtube_liked_videos", items)
        from .snapshot import get_snapshot_items

        scan_items = get_snapshot_items(session, snap.id)
        music_like = sum(1 for it in scan_items if it.is_music_candidate)
        unavailable = sum(1 for it in scan_items if it.is_available is False)
        console.print(
            f"[green]✓[/green] Snapshot [bold]#{snap.id}[/bold] stored "
            f"({len(items)} videos, [bold]{music_like}[/bold] music-like, "
            f"[bold]{unavailable}[/bold] unavailable)."
        )
```

- [ ] **Step 9: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green. The `attach_video_statuses` helper covers the stage-2 + injection logic; the only untested surface is the thin Typer wrapper, which manual e2e verifies.

- [ ] **Step 10: Commit**

```bash
git add src/likesurgeon/youtube_client.py src/likesurgeon/cli.py tests/test_youtube_client.py
git commit -m "feat(scan): wire ghost detection into scan youtube-likes (stage-2 + injection)"
```

---

## Task 6: `latest_snapshots_for_source` helper

**Files:**
- Modify: `src/likesurgeon/snapshot.py` (add helper)
- Modify: `tests/test_snapshot.py` (append tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_snapshot.py`:

```python
def test_latest_snapshots_for_source_returns_most_recent_first(session) -> None:
    """Helper returns up to `limit` snapshots of the given source ordered
    most-recent first."""
    from likesurgeon.snapshot import create_snapshot, latest_snapshots_for_source

    yt_items = [{
        "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v1"}},
        "contentDetails": {"videoId": "v1"},
    }]
    snap_a = create_snapshot(session, "youtube_liked_videos", yt_items)
    snap_b = create_snapshot(session, "youtube_liked_videos", yt_items)
    snap_c = create_snapshot(session, "youtube_liked_videos", yt_items)

    out = latest_snapshots_for_source(session, "youtube_liked_videos", limit=2)
    assert [s.id for s in out] == [snap_c.id, snap_b.id]


def test_latest_snapshots_for_source_filters_by_source(session) -> None:
    """Only snapshots of the requested source are returned."""
    from likesurgeon.snapshot import create_snapshot, latest_snapshots_for_source

    create_snapshot(session, "youtube_liked_videos", [{
        "snippet": {"title": "Y", "channelTitle": "C", "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }])
    create_snapshot(session, "ytmusic_liked_songs", [{
        "videoId": "ytm",
        "title": "T",
        "artists": [{"name": "A"}],
    }])

    out = latest_snapshots_for_source(session, "ytmusic_liked_songs", limit=5)
    assert len(out) == 1
    assert out[0].source == "ytmusic_liked_songs"


def test_latest_snapshots_for_source_returns_empty_when_no_snapshots(session) -> None:
    from likesurgeon.snapshot import latest_snapshots_for_source

    assert latest_snapshots_for_source(session, "youtube_liked_videos", limit=2) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_snapshot.py -v -k "latest_snapshots_for_source"
```

Expected: FAIL with `ImportError: cannot import name 'latest_snapshots_for_source'`.

- [ ] **Step 3: Implement the helper**

In `src/likesurgeon/snapshot.py`, after the existing `latest_snapshot` function (around line 243):

```python
def latest_snapshots_for_source(
    session: Session, source: str, *, limit: int = 2
) -> list[Snapshot]:
    """Return up to ``limit`` most-recent snapshots for ``source``,
    most-recent first. Used by drift detection in `compare-likes` to
    pull the (latest, previous) pair per source. Returns ``[]`` when the
    source has no snapshots; returns a 1-list when only one exists.
    """
    stmt = (
        select(Snapshot)
        .where(Snapshot.source == source)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_snapshot.py -v -k "latest_snapshots_for_source"
```

Expected: 3 PASS.

- [ ] **Step 5: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): add latest_snapshots_for_source(session, source, *, limit) helper"
```

---

## Task 7: `drift.py` module

**Files:**
- Create: `src/likesurgeon/drift.py`
- Create: `tests/test_drift.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_drift.py`:

```python
"""Tests for drift detection — snapshot-pair metadata comparison."""

from __future__ import annotations

import json
from typing import Any

import pytest


class _FakeSnapshotItem:
    """Duck-typed stand-in for SnapshotItem in pure-function tests."""

    def __init__(
        self,
        *,
        id: int,
        video_id: str | None,
        title: str,
        artists: list[str],
    ) -> None:
        self.id = id
        self.video_id = video_id
        self.title = title
        self.artists = json.dumps(artists)


def test_detect_drift_flags_significant_title_change() -> None:
    """token_sort_ratio < 0.90 → drift finding."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(
        id=1, video_id="v", title="Blueming (Official MV)", artists=["IU"]
    )]
    curr = [_FakeSnapshotItem(
        id=2, video_id="v", title="아이유 - Blueming Remastered 2024 [4K]", artists=["IU"]
    )]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert len(findings) == 1
    f = findings[0]
    assert f.video_id == "v"
    assert f.prev_snapshot_item_id == 1
    assert f.curr_snapshot_item_id == 2
    assert f.title_similarity < 0.9


def test_detect_drift_flags_artists_change_even_with_same_title() -> None:
    """Channel rename / `- Topic` migration keeps title but changes artists."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Same", artists=["1theK"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Same", artists=["IU - Topic"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert len(findings) == 1
    assert findings[0].artists_changed is True


def test_detect_drift_ignores_cosmetic_changes() -> None:
    """token_sort_ratio >= 0.90 AND artists unchanged → no finding."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Hello World", artists=["A"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Hello, World!", artists=["A"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert findings == []


def test_detect_drift_ignores_artist_order() -> None:
    """Artists list order changes alone (e.g. ['A', 'B'] → ['B', 'A']) is not drift —
    use set-based comparison after normalisation."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id="v", title="Same", artists=["A", "B"])]
    curr = [_FakeSnapshotItem(id=2, video_id="v", title="Same", artists=["B", "A"])]

    findings = detect_drift(prev, curr, source="youtube_liked_videos")
    assert findings == []


def test_detect_drift_silently_skips_unmatched_video_ids() -> None:
    """Items in curr without a matching prev (new likes) are not drift."""
    from likesurgeon.drift import detect_drift

    prev: list[Any] = []
    curr = [_FakeSnapshotItem(id=1, video_id="new", title="X", artists=["Y"])]

    assert detect_drift(prev, curr, source="youtube_liked_videos") == []


def test_detect_drift_ignores_items_without_video_id() -> None:
    """SnapshotItem.video_id may be None (rare YT Music edge case) — those
    can't be drift-keyed."""
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=1, video_id=None, title="X", artists=["Y"])]
    curr = [_FakeSnapshotItem(id=2, video_id=None, title="Z", artists=["W"])]

    assert detect_drift(prev, curr, source="ytmusic_liked_songs") == []


def test_detect_drift_finding_carries_source_and_artists_lists() -> None:
    from likesurgeon.drift import detect_drift

    prev = [_FakeSnapshotItem(id=10, video_id="v", title="Old", artists=["A"])]
    curr = [_FakeSnapshotItem(id=20, video_id="v", title="New Title Entirely", artists=["B"])]

    findings = detect_drift(prev, curr, source="ytmusic_liked_songs")
    assert findings[0].source == "ytmusic_liked_songs"
    assert findings[0].prev_artists == ("A",)
    assert findings[0].curr_artists == ("B",)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_drift.py -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `drift.py`**

Create `src/likesurgeon/drift.py`:

```python
"""Snapshot-pair drift detection.

Pure module: takes two `SnapshotItem` lists (prev and curr from the same
source) and returns `DriftFinding` rows where same-`video_id` items have
meaningfully different metadata. Used by `compare-likes` to surface
"the underlying video was retitled / channel renamed / `-Topic`
migrated" between scans.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rapidfuzz.fuzz import token_sort_ratio

_TITLE_SIMILARITY_THRESHOLD = 0.90


@dataclass(frozen=True)
class DriftFinding:
    """One same-video_id pair where metadata moved between snapshots."""

    source: str
    video_id: str
    prev_snapshot_item_id: int
    curr_snapshot_item_id: int
    prev_title: str
    curr_title: str
    prev_artists: tuple[str, ...]
    curr_artists: tuple[str, ...]
    title_similarity: float
    artists_changed: bool


def _decode_artists(value: Any) -> tuple[str, ...]:
    """SnapshotItem.artists is a JSON-encoded list[str]; decode it
    defensively (NULL, empty string, malformed → empty tuple)."""
    if not value:
        return ()
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(x) for x in decoded)


def _normalize_artists_set(artists: tuple[str, ...]) -> frozenset[str]:
    """Order-independent normalised artist comparison: lowercase + strip,
    drop empties, return as a frozenset so swap-only diffs (`['A','B']`
    vs `['B','A']`) are not flagged as drift."""
    return frozenset(a.strip().lower() for a in artists if a.strip())


def detect_drift(
    prev: list[Any], curr: list[Any], *, source: str
) -> list[DriftFinding]:
    """Return drift findings for items present in both snapshots by video_id.

    A finding is emitted when EITHER:
      * `token_sort_ratio(prev.title, curr.title) / 100 < 0.90` — the title
        moved beyond cosmetic punctuation/whitespace tolerance, OR
      * the (set-normalised) artists list changed at all — channel
        renames, `- Topic` migrations, attribution edits.

    Items present in only one of `prev` / `curr` are not drift (those are
    new likes / unlikes, surfaced elsewhere). Items lacking `video_id`
    (rare YT Music edge case where the only handle is `canonical_key`)
    can't be drift-keyed and are skipped.
    """
    prev_by_id = {p.video_id: p for p in prev if p.video_id}
    findings: list[DriftFinding] = []
    for c in curr:
        if not c.video_id:
            continue
        p = prev_by_id.get(c.video_id)
        if p is None:
            continue
        title_sim = token_sort_ratio(p.title, c.title) / 100.0
        prev_artists = _decode_artists(p.artists)
        curr_artists = _decode_artists(c.artists)
        artists_changed = _normalize_artists_set(prev_artists) != _normalize_artists_set(
            curr_artists
        )
        if title_sim < _TITLE_SIMILARITY_THRESHOLD or artists_changed:
            findings.append(
                DriftFinding(
                    source=source,
                    video_id=c.video_id,
                    prev_snapshot_item_id=p.id,
                    curr_snapshot_item_id=c.id,
                    prev_title=p.title,
                    curr_title=c.title,
                    prev_artists=prev_artists,
                    curr_artists=curr_artists,
                    title_similarity=title_sim,
                    artists_changed=artists_changed,
                )
            )
    return findings
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_drift.py -v
```

Expected: 7 PASS.

- [ ] **Step 5: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/drift.py tests/test_drift.py
git commit -m "feat(drift): add detect_drift module (title fuzzy + artists set comparison)"
```

---

## Task 8: compare-likes integration (ghost + drift wiring)

**Files:**
- Modify: `src/likesurgeon/diagnosis.py` (add `ISSUE_UNAVAILABLE_VIDEO`, `ISSUE_METADATA_DRIFT`, persistence helpers)
- Modify: `src/likesurgeon/cli.py:378-...` (`compare_likes_cmd`) — add `_PipelineResult` dataclass + `_compare_and_persist` orchestrator that pulls (latest, prev) snapshot pairs per source, runs the new finders, and extends the existing summary table with two rows. (`compare.py` itself is not modified.)
- Modify: `tests/test_compare.py` (append integration tests)

- [ ] **Step 1: Write failing integration tests**

Append to `tests/test_compare.py` (preserve existing imports/tests):

```python
def test_compare_likes_persists_unavailable_video_findings(session) -> None:
    """One youtube_liked_videos snapshot with one is_available=False item
    → DiagnosisItem(issue_type='unavailable_video') is persisted."""
    from likesurgeon.cli import _compare_and_persist  # introduced in this task
    from likesurgeon.diagnosis import ISSUE_UNAVAILABLE_VIDEO
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    create_snapshot(session, "ytmusic_liked_songs", [{
        "videoId": "ytm",
        "title": "T",
        "artists": [{"name": "A"}],
    }])
    create_snapshot(session, "youtube_liked_videos", [{
        "snippet": {"title": "T", "channelTitle": "A", "resourceId": {"videoId": "v1"}},
        "contentDetails": {"videoId": "v1"},
        "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"},
    }])

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_UNAVAILABLE_VIDEO)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].confidence == 1.0
    assert "video unavailable: deleted" in rows[0].reason


def test_compare_likes_persists_metadata_drift_findings(session) -> None:
    """Two YouTube snapshots of the same source with same video_id but
    different title → DiagnosisItem(issue_type='metadata_drift')."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    # Two YT snapshots, same video_id, different title.
    create_snapshot(session, "youtube_liked_videos", [{
        "snippet": {"title": "Original Title", "channelTitle": "C",
                    "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }])
    create_snapshot(session, "youtube_liked_videos", [{
        "snippet": {"title": "Completely Different Now [Remastered 2024]",
                    "channelTitle": "C", "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }])
    # YT Music snapshot so compare-likes has both sources to compare.
    create_snapshot(session, "ytmusic_liked_songs", [{
        "videoId": "ytm", "title": "T", "artists": [{"name": "A"}],
    }])

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_METADATA_DRIFT)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].confidence == 1.0
    # Reason carries source, snapshot-item ids, sim, and artists.
    assert "source=youtube_liked_videos" in rows[0].reason
    assert "prev_item=" in rows[0].reason
    assert "curr_item=" in rows[0].reason
    assert "title:" in rows[0].reason


def test_compare_likes_drift_silently_skips_when_only_one_snapshot(session) -> None:
    """Source with only 1 snapshot → no drift finding for it (no error)."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.diagnosis import ISSUE_METADATA_DRIFT
    from likesurgeon.models import DiagnosisItem
    from likesurgeon.snapshot import create_snapshot

    create_snapshot(session, "youtube_liked_videos", [{
        "snippet": {"title": "T", "channelTitle": "C", "resourceId": {"videoId": "v"}},
        "contentDetails": {"videoId": "v"},
    }])
    create_snapshot(session, "ytmusic_liked_songs", [{
        "videoId": "ytm", "title": "T", "artists": [{"name": "A"}],
    }])

    diagnosis_id = _compare_and_persist(session).diagnosis_id
    rows = (
        session.query(DiagnosisItem)
        .filter_by(diagnosis_id=diagnosis_id, issue_type=ISSUE_METADATA_DRIFT)
        .all()
    )
    assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_compare.py -v -k "unavailable_video or metadata_drift or drift_silently_skips"
```

Expected: FAIL — `_compare_and_persist`, `ISSUE_UNAVAILABLE_VIDEO`, `ISSUE_METADATA_DRIFT` not defined.

- [ ] **Step 3: Add new issue_type constants and persistence helpers in diagnosis.py**

The existing persistence entry point is `create_diagnosis(session, inp: DiagnosisInput) -> Diagnosis` (not `persist_findings`). Don't change its signature; instead add two new sibling helpers that take a `diagnosis_id` (so they can be called *after* `create_diagnosis` has flushed the row) and return ready-to-add `DiagnosisItem` instances. The orchestrator in Step 4 wires them together.

In `src/likesurgeon/diagnosis.py`, after the existing `ISSUE_YTMUSIC_ONLY` line:

```python
ISSUE_UNAVAILABLE_VIDEO = "unavailable_video"
ISSUE_METADATA_DRIFT = "metadata_drift"
```

After `_match_item` (around line 100), add:

```python
def build_unavailable_video_items(
    diagnosis_id: int, snapshot_items: list[Any]
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows for SnapshotItems with is_available=False.

    ``confidence=1.0`` because availability is a fact, not a probability —
    using a lower confidence would let ``--min-confidence`` filters hide
    real ghosts.
    """
    out: list[DiagnosisItem] = []
    for item in snapshot_items:
        if item.is_available is not False:
            continue
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=ISSUE_UNAVAILABLE_VIDEO,
                confidence=1.0,
                reason=f"video unavailable: {item.unavailable_reason}",
                source_track_id=item.track_id,
                related_track_id=None,
                status="open",
            )
        )
    return out


def build_metadata_drift_items(
    diagnosis_id: int,
    findings: list[Any],
    curr_items: list[Any],
) -> list[DiagnosisItem]:
    """Build DiagnosisItem rows from DriftFinding objects.

    ``confidence=1.0`` because each finding is a deterministic post-filter
    output (the threshold check happened in ``detect_drift``). Severity goes
    in ``reason`` text, NOT in ``confidence`` — using ``title_similarity``
    as confidence would invert ``--min-confidence`` semantics (lower sim =
    bigger drift = would get filtered out).

    ``related_track_id`` is left ``None`` because the prev/curr ``Track``
    rows are usually the same row (Tracks get upserted with latest metadata
    on every scan). The durable prev/curr identifiers are the
    ``SnapshotItem`` ids embedded in ``reason``.
    """
    curr_by_item_id = {it.id: it for it in curr_items}
    out: list[DiagnosisItem] = []
    for f in findings:
        curr = curr_by_item_id.get(f.curr_snapshot_item_id)
        track_id = curr.track_id if curr is not None else None
        reason = (
            f"source={f.source}; "
            f"prev_item={f.prev_snapshot_item_id}, curr_item={f.curr_snapshot_item_id}; "
            f"title: {f.prev_title!r} → {f.curr_title!r} (sim {f.title_similarity:.2f}); "
            f"artists: {list(f.prev_artists)} → {list(f.curr_artists)}"
        )
        out.append(
            DiagnosisItem(
                diagnosis_id=diagnosis_id,
                issue_type=ISSUE_METADATA_DRIFT,
                confidence=1.0,
                reason=reason,
                source_track_id=track_id,
                related_track_id=None,
                status="open",
            )
        )
    return out
```

Imports to add at the top of `diagnosis.py`: `from typing import Any` (used by both helpers' parameter annotations — `diagnosis.py` doesn't import it today). `DiagnosisItem` is already imported.

- [ ] **Step 4: Add `_compare_and_persist` orchestrator in cli.py**

In `src/likesurgeon/cli.py`, locate `compare_likes_cmd` (around line 378) and the surrounding scaffolding it already uses (it imports `compare_likes`, `CompareInput`, `create_diagnosis`, `DiagnosisInput`, `latest_snapshot`, `get_snapshot_items`, and prints a `Table` summary at the end — preserve all that). Add a testable inner orchestrator `_compare_and_persist(session) -> _PipelineResult` and make `compare_likes_cmd` a wrapper that uses the existing `Table` output, extended with two new rows for the new finding types:

First add `CompareResult` to the module-top imports in `cli.py` so the dataclass annotation can reference it directly (ruff's `UP037` flags quoted annotations under `from __future__ import annotations`, and a forward-reference string would fire it). Find the existing `from .compare import ...` block at the top of the file and add `CompareResult`:

```python
from .compare import CompareInput, CompareResult, compare_likes
```

If those names are currently imported only inside `compare_likes_cmd` (the function-local import on line 380), promote them to the top instead — keeping all three together avoids `F811` redefinition warnings and makes the dataclass annotation legal.

Then add the dataclass and the orchestrator at module scope (above `compare_likes_cmd`):

```python
@dataclass(frozen=True)
class _PipelineResult:
    """Compound return value for `_compare_and_persist` so the CLI wrapper
    can render the existing summary table AND the two new finding-type
    rows from a single call. Tests typically only need `.diagnosis_id`.
    """

    diagnosis_id: int
    compare_result: CompareResult
    unavailable_count: int
    drift_count: int


def _compare_and_persist(session: Session) -> _PipelineResult:
    """Run the full 0.3 compare-likes pipeline against the current session
    and return the persisted Diagnosis id plus the finding counts the CLI
    summary needs.

    Pipeline:
      1. Cross-source matcher (existing 0.2 buckets) → CompareResult.
      2. Persist a Diagnosis with the existing buckets via `create_diagnosis`.
      3. Append ghost findings (latest YouTube snapshot's is_available=False rows).
      4. Append drift findings per source (latest, prev) via `detect_drift`.
      All findings live on a single Diagnosis row.
    """
    from .diagnosis import (
        DiagnosisInput,
        build_metadata_drift_items,
        build_unavailable_video_items,
        create_diagnosis,
    )
    from .drift import detect_drift
    from .snapshot import (
        YOUTUBE_LIKED_VIDEOS,
        YTMUSIC_LIKED_SONGS,
        get_snapshot_items,
        latest_snapshot,
        latest_snapshots_for_source,
    )

    yt_snap = latest_snapshot(session, source=YOUTUBE_LIKED_VIDEOS)
    ytm_snap = latest_snapshot(session, source=YTMUSIC_LIKED_SONGS)
    if yt_snap is None or ytm_snap is None:
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

    yt_items = get_snapshot_items(session, yt_snap.id)
    ytm_items = get_snapshot_items(session, ytm_snap.id)

    # Stage A — existing cross-source matcher.
    cmp_result = compare_likes(CompareInput(ytmusic=ytm_items, youtube=yt_items))

    # Stage B — persist Diagnosis + the existing 0.2 finding buckets.
    diagnosis = create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=cmp_result,
        ),
    )

    # Stage C — append ghost findings.
    ghost_items = build_unavailable_video_items(diagnosis.id, yt_items)
    for it in ghost_items:
        session.add(it)

    # Stage D — append drift findings per source against (latest, prev).
    drift_total = 0
    for source in (YOUTUBE_LIKED_VIDEOS, YTMUSIC_LIKED_SONGS):
        snaps = latest_snapshots_for_source(session, source, limit=2)
        if len(snaps) < 2:
            continue
        curr_snap, prev_snap = snaps[0], snaps[1]
        curr_items = get_snapshot_items(session, curr_snap.id)
        prev_items = get_snapshot_items(session, prev_snap.id)
        findings = detect_drift(prev_items, curr_items, source=source)
        drift_items = build_metadata_drift_items(diagnosis.id, findings, curr_items)
        drift_total += len(drift_items)
        for it in drift_items:
            session.add(it)

    session.flush()
    return _PipelineResult(
        diagnosis_id=diagnosis.id,
        compare_result=cmp_result,
        unavailable_count=len(ghost_items),
        drift_count=drift_total,
    )


@app.command("compare-likes")
def compare_likes_cmd() -> None:
    """Compare latest YouTube Music vs. YouTube liked-videos snapshots."""
    _, factory = _bootstrap()
    with session_scope(factory) as session:
        outcome = _compare_and_persist(session)

    result = outcome.compare_result
    console.print(f"[green]✓[/green] Diagnosis [bold]#{outcome.diagnosis_id}[/bold] saved.")
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
    table.add_row("Unavailable videos (ghost)", str(outcome.unavailable_count))
    table.add_row("Metadata drift candidates", str(outcome.drift_count))
    console.print(table)
    console.print("Run [cyan]likesurgeon issues[/cyan] for the full per-item breakdown.")
```

**Imports to add at the top of `cli.py`** for this task:
- `from dataclasses import dataclass` — new in this file (used by `_PipelineResult`).
- `from sqlalchemy.orm import Session` — if not already present (used by `_compare_and_persist`'s parameter annotation).
- `from .compare import CompareInput, CompareResult, compare_likes` — promote the existing function-local import so `CompareResult` is in scope for the dataclass annotation. (Quoting it (`"CompareResult"`) would also work but ruff `UP037` flags quoted annotations under `from __future__ import annotations`, which the file already has.)

**Imports to remove from `compare_likes_cmd`'s function body**: the existing `from .compare import CompareInput, compare_likes` (cli.py:380) — gone because the wrapper above doesn't construct `CompareInput` itself anymore (it's done inside `_compare_and_persist`, which uses the now-module-top names). Don't re-import `CompareResult` / `CompareInput` / `compare_likes` *inside* `_compare_and_persist` either; they're already at module-top. (Re-importing them locally would trip ruff `F811` "redefined while unused" — a real lint failure.)

The source-string constants `YTMUSIC_LIKED_SONGS` / `YOUTUBE_LIKED_VIDEOS` live in `snapshot.py` (NOT `models.py`), so the import path in this file is `from .snapshot import ...`.

Run `uv run ruff check .` after this step to confirm — the new dataclass + future-annotations combination is the most likely lint trip-point in this task.

- [ ] **Step 5: Run integration tests to verify they pass**

```bash
uv run pytest tests/test_compare.py -v -k "unavailable_video or metadata_drift or drift_silently_skips"
```

Expected: 3 PASS.

- [ ] **Step 6: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/likesurgeon/diagnosis.py src/likesurgeon/cli.py tests/test_compare.py
git commit -m "feat(compare): wire ghost + drift into compare-likes pipeline"
```

---

## Task 9: Doctor counts + issues help text + README

**Files:**
- Modify: `src/likesurgeon/doctor.py` (add `unavailable_videos`, `metadata_drift` to `DiagnosisSummary`, count them in `_latest_diagnosis_summary`)
- Modify: `src/likesurgeon/cli.py` (extend the `doctor()` command's diagnosis-summary print string with the two new counts; update `issues --type` help text from 3 → 5 types)
- Modify: `tests/test_doctor.py` (count aggregation test)
- Modify: `README.md`

- [ ] **Step 1: Write failing doctor tests**

Append to `tests/test_doctor.py`:

```python
def test_doctor_summary_counts_new_issue_types(session) -> None:
    """`unavailable_videos` and `metadata_drift` counts come from the
    DiagnosisSummary aggregation alongside the existing types."""
    from likesurgeon.cli import _compare_and_persist
    from likesurgeon.doctor import health_summary
    from likesurgeon.snapshot import create_snapshot

    # YT Music snapshot.
    create_snapshot(session, "ytmusic_liked_songs", [{
        "videoId": "ytm", "title": "T", "artists": [{"name": "A"}],
    }])
    # Two YT snapshots: first establishes prev, second adds a ghost AND a drift.
    create_snapshot(session, "youtube_liked_videos", [
        {"snippet": {"title": "Original", "channelTitle": "C",
                     "resourceId": {"videoId": "v"}},
         "contentDetails": {"videoId": "v"}},
    ])
    create_snapshot(session, "youtube_liked_videos", [
        {"snippet": {"title": "New Title Entirely Different",
                     "channelTitle": "C",
                     "resourceId": {"videoId": "v"}},
         "contentDetails": {"videoId": "v"},
         "_likesurgeon_video_status": {"is_available": False, "reason": "deleted"}},
    ])

    _compare_and_persist(session)
    report = health_summary(session)
    assert report.latest_diagnosis is not None
    assert report.latest_diagnosis.unavailable_videos >= 1
    assert report.latest_diagnosis.metadata_drift >= 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_doctor.py -v -k "doctor_summary_counts_new"
```

Expected: FAIL — `DiagnosisSummary` doesn't have those fields.

- [ ] **Step 3: Add fields to `DiagnosisSummary` and aggregation**

In `src/likesurgeon/doctor.py`, locate `DiagnosisSummary` (around line 31). Add two fields, matching the existing field style (no `created_at` — that field doesn't exist in the current dataclass):

```python
@dataclass(frozen=True)
class DiagnosisSummary:
    diagnosis_id: int
    possibly_missing_from_ytmusic: int
    pointer_drift: int
    ytmusic_only: int
    unavailable_videos: int
    metadata_drift: int
```

In `_latest_diagnosis_summary` (around line 79), extend the local-import block and the `counts` dict initialiser, and pass the two new fields to the `DiagnosisSummary(...)` construction:

```python
    from .diagnosis import (
        ISSUE_METADATA_DRIFT,
        ISSUE_POINTER_DRIFT,
        ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
        ISSUE_UNAVAILABLE_VIDEO,
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
        ISSUE_UNAVAILABLE_VIDEO: 0,
        ISSUE_METADATA_DRIFT: 0,
    }
    for it in items:
        if it.issue_type in counts:
            counts[it.issue_type] += 1
    summary = DiagnosisSummary(
        diagnosis_id=diag.id,
        possibly_missing_from_ytmusic=counts[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC],
        pointer_drift=counts[ISSUE_POINTER_DRIFT],
        ytmusic_only=counts[ISSUE_YTMUSIC_ONLY],
        unavailable_videos=counts[ISSUE_UNAVAILABLE_VIDEO],
        metadata_drift=counts[ISSUE_METADATA_DRIFT],
    )
```

(Keep the rest of the function — match-rate computation, return tuple — unchanged.)

The user-facing render is NOT in `doctor.py` — it's in `src/likesurgeon/cli.py` inside the `@app.command()` `doctor()` function (around line 333). That function calls `health_summary(session)` and prints the result via Rich `console.print` calls. Locate the existing line that renders the latest-diagnosis bucket counts:

```python
        d = report.latest_diagnosis
        console.print(
            f"[bold]Latest diagnosis #{d.diagnosis_id}:[/bold] "
            f"{d.possibly_missing_from_ytmusic} possibly missing from YT Music · "
            f"{d.pointer_drift} pointer drift · "
            f"{d.ytmusic_only} YT Music only"
        )
```

Extend the same string to include the two new counts:

```python
        d = report.latest_diagnosis
        console.print(
            f"[bold]Latest diagnosis #{d.diagnosis_id}:[/bold] "
            f"{d.possibly_missing_from_ytmusic} possibly missing from YT Music · "
            f"{d.pointer_drift} pointer drift · "
            f"{d.ytmusic_only} YT Music only · "
            f"{d.unavailable_videos} unavailable videos · "
            f"{d.metadata_drift} metadata drift"
        )
```

The acceptance criterion is that `uv run likesurgeon doctor` shows the two new counts on the same diagnosis-summary line as the existing three.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_doctor.py -v -k "doctor_summary_counts_new"
```

Expected: PASS.

- [ ] **Step 5: Update `issues --type` help text**

In `src/likesurgeon/cli.py`, locate the `issues` command (around line 435). Find the `--type` `help=` string (currently lists 3 types) and replace it to enumerate all 5:

```python
            help=(
                "Filter findings by issue type. One of: "
                "possibly_missing_from_ytmusic | possible_pointer_drift | "
                "ytmusic_only | unavailable_video | metadata_drift."
            ),
```

- [ ] **Step 6: README updates**

Edit `README.md`:

a. **"What it does today"** — add a bullet near the existing `compare-likes` line:

```markdown
- Detects ghost YouTube likes (deleted, made private, unavailable) at scan time, and metadata drift (title or artists list changes) between snapshots — both surface through `compare-likes` and `issues`.
```

b. **Roadmap row** — change the 0.3 row to:

```markdown
| **0.3**     | Matching engine: ghost YouTube likes (deleted/private/unavailable, detected at `scan youtube-likes` time via `videos.list`) and metadata drift (snapshot-pair title/artists comparison via [RapidFuzz](https://github.com/maxbachmann/RapidFuzz)). Both surface as new `issue_type` rows on `compare-likes`; no new commands. |
```

c. **`### 5. Compare across sources`** — add one example demonstrating the new types:

```markdown
uv run likesurgeon issues --type unavailable_video
uv run likesurgeon issues --type metadata_drift
```

d. **Delete the entire `## Upgrading from 0.1` section** (per spec — schema migrations are now in-place via `_migrate_in_place`, no user-visible upgrade step).

- [ ] **Step 7: Lint and full suite**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/likesurgeon/doctor.py src/likesurgeon/cli.py tests/test_doctor.py README.md
git commit -m "feat(doctor): surface unavailable_videos + metadata_drift counts; update issues help + README"
```

(The commit touches `cli.py` for both the `doctor()` render extension AND the `issues --type` help text update — both small string changes living next to each other in the same file.)

---

## Final verification

- [ ] **Test suite green:**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

- [ ] **Manual e2e (run on the actual user's machine, not a fake)**

```bash
# 1. Ensure auth is set up (`auth ytmusic --from-browser chrome` and `auth youtube`).
# 2. Fresh scans pick up the new ghost detection:
uv run likesurgeon scan ytmusic
uv run likesurgeon scan youtube-likes
#    Expected: youtube-likes summary now shows "<N> unavailable" count.

# 3. Re-scan youtube-likes to set up drift detection state:
uv run likesurgeon scan youtube-likes
#    Now there are 2 youtube snapshots — drift detection has a baseline.

# 4. Run the diagnosis pipeline:
uv run likesurgeon compare-likes

# 5. Inspect the new finding types:
uv run likesurgeon issues --type unavailable_video
uv run likesurgeon issues --type metadata_drift
#    Expected: rows for any actually-deleted videos in your LL, and any
#    drift findings between the two recent youtube snapshots. (`issues`
#    has no `--limit` flag in 0.2; if you want capped output, pipe through
#    `head` or add `--format json | jq` and slice. `--limit` becomes a
#    candidate for 0.3.x if listings get unwieldy in practice.)

# 6. Doctor surfaces the new counts:
uv run likesurgeon doctor
#    Expected: the diagnosis-summary line now ends with "<N> unavailable
#    videos · <M> metadata drift" alongside the existing three counts on
#    the same line. (compare-likes' Table output gets two genuinely new
#    rows; doctor extends its single-line summary instead.)

# 7. Migration verification — pick an existing 0.2-shape DB if you have one
#    (or restore from a backup) and run any command. The inline migration
#    must add the two new columns transparently:
sqlite3 ~/.like-surgeon/like-surgeon.sqlite "PRAGMA table_info(snapshot_items);" | grep -E "is_available|unavailable_reason"
#    Expected: both columns listed. If you see them after running just one
#    likesurgeon command, _migrate_in_place is wired correctly.
```

- [ ] **Push branch and open PR**

```bash
git push -u origin feat/0.3-matching-engine
gh pr create --base main --title "feat: 0.3 matching engine — ghost detection + metadata drift" --body "$(cat <<'EOF'
## Summary

Adds two diagnosis dimensions on top of the existing cross-source matcher, surfaced through `compare-likes` / `issues` / `doctor` without any new CLI commands.

- **Ghost detection**: `scan youtube-likes` calls `videos.list?part=status` and persists `SnapshotItem.is_available` / `unavailable_reason`. Two-stage status pipeline — `fetch_video_statuses` returns a `missing_from_videos_list` placeholder for IDs absent from the response; the scan command disambiguates against `playlistItems.list` snippet titles (`"Private video"` / `"Deleted video"` / other → `"private"` / `"deleted"` / `"unavailable"`). YouTube only.
- **Drift detection**: snapshot-pair comparison on title (`token_sort_ratio < 0.90`) and artists list (set-normalised). Both YouTube and YT Music. Findings carry durable `SnapshotItem` ids in `reason` text — diagnostic only, not a parseable contract.
- **Schema migration**: inline additive `ALTER TABLE` at engine construction (`_migrate_in_place` in db.py). Transparent for 0.2.x users; no DB wipe.

Per-task commits follow the [implementation plan](docs/superpowers/plans/2026-05-07-likesurgeon-0.3-matching-engine.md). Design rationale (including false-ghost analysis for returned private videos, batch failure policy, and confidence-vs-similarity discussion) lives in [docs/superpowers/specs/2026-05-07-likesurgeon-0.3-matching-engine-design.md](docs/superpowers/specs/2026-05-07-likesurgeon-0.3-matching-engine-design.md).

## Test plan

- [x] All unit + integration tests green (`uv run pytest -q`).
- [x] `ruff check` + `ruff format --check` clean.
- [x] Manual e2e: scanned YT likes twice in a row on a real account; `compare-likes` produced both new finding types; `doctor` surfaced the counts; `issues --type unavailable_video` / `--type metadata_drift` filtered correctly.
- [x] Migration test against a 0.2-shape SQLite DB — `is_available` and `unavailable_reason` columns appeared transparently after the first command run.
EOF
)"
```
