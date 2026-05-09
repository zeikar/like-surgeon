# likesurgeon 0.3.1 — Region-aware ghost detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface region-blocked YouTube videos as ghosts so the existing `unavailable_video` finding type catches them. Today 0.3 only inspects `videos.list?part=status` (`uploadStatus = rejected/deleted`), missing region-restricted videos that play `processed`/`public` to the API but are blocked for the user's region.

**Architecture:** Extend the existing `videos.list` call to also fetch `part=contentDetails`, thread a configured `user_region` through `_classify_status`, and add a new `Config.region` field loaded from `~/.like-surgeon/config.json` with strict ISO 3166-1 alpha-2 validation. CLI gains a `--region KR` one-off override (never written to disk). Invalid region values exit code 2 before any API call. Quota cost is unchanged (`videos.list` is 1 unit/call regardless of `part=` selection).

**Tech Stack:** Python 3.11+, Typer (existing), pytest (existing), ruff (existing). No new deps.

**Branch:** Already on `feat/0.3.1-region-aware-ghost` off `main`. Current HEAD is `b81ba96` (the spec commit). The working tree carries one uncommitted change to `src/likesurgeon/cli.py` (the `issues` command's video_id output, a pre-existing carry described in the spec's "Carried changes" section). Task 1 lands that carry as the first commit on the branch.

**Spec:** [docs/superpowers/specs/2026-05-09-likesurgeon-0.3.1-region-aware-ghost-design.md](../specs/2026-05-09-likesurgeon-0.3.1-region-aware-ghost-design.md)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/likesurgeon/config.py` | Add `region: str \| None` field, `_load_region`, `_validate_region`, `InvalidRegionError`. New imports: `json`, `re` |
| `src/likesurgeon/youtube_client.py` | Update `_classify_status` signature to `(status, content_details, user_region)`, change `_videos_list` part from `"status"` to `"status,contentDetails"`, add `user_region` keyword to `fetch_video_statuses` and pass it to `_classify_status` |
| `src/likesurgeon/cli.py` | Add module-level `_resolve_region(cli, config) -> str \| None`, `_parse_region_flag(value) -> str \| None`. Add `--region` CLI flag on `scan_youtube_likes` and wire user_region + unset warning. Catch `InvalidRegionError` in `_bootstrap()` |
| `tests/test_config.py` | NEW: `_validate_region` (6 cases) + `_load_region` (8 cases including the strict-alpha-2 raise) |
| `tests/test_youtube_client.py` | Append: 5 `_classify_status` region cases + 1 `fetch_video_statuses` pass-through |
| `tests/test_cli.py` | NEW: `_resolve_region` (3) + 4 `scan_youtube_likes` wiring tests via `CliRunner` + 1 `Config.load` raise + 1 `_bootstrap` convert |
| `README.md` | Status line bump to MVP 0.3.1, "What it does today" bullet for region detection, roadmap `**0.3.1**` row, "Local layout" `config.json` entry, config setup snippet, caveat quota note updated to reference `part=status,contentDetails` |
| `pyproject.toml` + `src/likesurgeon/__init__.py` | Bump version `0.3.0` → `0.3.1` |

CLI test harness pattern (used by Task 7 + Task 8): set `LIKE_SURGEON_HOME` env var to a `tmp_path`, monkeypatch `likesurgeon.youtube_client.YouTubeClient` with a `FakeYouTubeClient` that captures the `user_region` keyword. The fake `fetch_video_statuses` returns canned `VideoStatus(is_available=True, reason=None)` for every input id.

---

## Task 1: Land the carried `issues` video_id change

**Files:**
- Modify: `src/likesurgeon/cli.py` (already modified in working tree pre-branch)

This task does NOT follow the TDD shape because the carry is pre-existing work from the previous session — already implemented in the working tree. The change is described in the spec's "Carried changes" section. We commit it first so subsequent TDD commits stay focused on the region work.

- [ ] **Step 1: Verify the working-tree change is still present**

```bash
git status --porcelain
```

Expected: includes the line `" M src/likesurgeon/cli.py"` (the carry). The plan file itself may also appear as `"?? docs/superpowers/plans/2026-05-09-likesurgeon-0.3.1-region-aware-ghost.md"` (untracked) — that's expected and gets committed via a separate path (operator decision; not part of this task). Concretely:

```bash
git status --porcelain | grep -E "^ M src/likesurgeon/cli\.py$"
```

Expected: prints exactly that one line. If it prints nothing, the carry is missing — stop and check with the operator before proceeding.

- [ ] **Step 2: Verify existing tests still pass with the carried change**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green. If anything fails, stop — the carry was supposed to be regression-free per spec.

- [ ] **Step 3: Commit the carry**

```bash
git add src/likesurgeon/cli.py
git commit -m "feat(issues): show video_id columns in table + JSON output"
```

(No new tests are added for this change. The spec explicitly de-scopes test coverage for the carry: "If existing tests don't cover it, that's a separate follow-up.")

---

## Task 2: `_validate_region` + `InvalidRegionError`

**Files:**
- Modify: `src/likesurgeon/config.py` (add imports, `InvalidRegionError`, `_REGION_PATTERN`, `_validate_region`)
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for `_validate_region`**

Create `tests/test_config.py`. (Note: only `pytest` is needed for these validation tests. The `json` and `Path` imports are added in Task 3 when the loader tests need them — adding them here would trip ruff's `F401` unused-import check at Step 5.)

```python
"""Tests for config.py — region validation and config.json loader."""

from __future__ import annotations

import pytest


def test_validate_region_accepts_uppercase_alpha_2() -> None:
    from likesurgeon.config import _validate_region

    assert _validate_region("KR") == "KR"


def test_validate_region_normalizes_case_and_whitespace() -> None:
    from likesurgeon.config import _validate_region

    assert _validate_region("  kr  ") == "KR"


def test_validate_region_rejects_three_letter_code() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("KOR")


def test_validate_region_rejects_full_country_name() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("Korea")


def test_validate_region_rejects_empty_after_strip() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    with pytest.raises(InvalidRegionError):
        _validate_region("   ")


def test_validate_region_rejects_embedded_whitespace_or_punct() -> None:
    from likesurgeon.config import InvalidRegionError, _validate_region

    for bogus in ("K R", "K\nR", "K1"):
        with pytest.raises(InvalidRegionError):
            _validate_region(bogus)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config.py -v -k "validate_region"
```

Expected: FAIL with `ImportError: cannot import name '_validate_region' from 'likesurgeon.config'` (or `InvalidRegionError`).

- [ ] **Step 3: Implement `InvalidRegionError` + `_validate_region`**

Edit `src/likesurgeon/config.py`. Add `import re` to the existing import block at the top. (`import json` is added in Task 3 when `_load_region` needs it — adding it now would trip ruff's `F401`.) Then, after the existing module constants (around `ENV_HOME = "LIKE_SURGEON_HOME"`), insert:

```python
CONFIG_FILENAME = "config.json"

_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")


class InvalidRegionError(ValueError):
    """Raised when a region value is not ISO 3166-1 alpha-2 (^[A-Z]{2}$).

    Surfaced fail-fast at config load and CLI flag parse so a typo (e.g.
    ``KOREA``, ``kr\\nx``, an empty string after trim) can't silently
    disable region detection — that would let a user think they were
    checking region-blocks while every video skipped the check.
    """


def _validate_region(value: str) -> str:
    """Normalize-then-validate. Strips, uppercases, then enforces the
    strict alpha-2 shape. Raises ``InvalidRegionError`` on any failure.
    """
    norm = value.strip().upper()
    if not _REGION_PATTERN.match(norm):
        raise InvalidRegionError(
            f"region must be an ISO 3166-1 alpha-2 country code (e.g. 'KR'), got {value!r}"
        )
    return norm
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v -k "validate_region"
```

Expected: 6 PASS.

- [ ] **Step 5: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/config.py tests/test_config.py
git commit -m "feat(config): add InvalidRegionError and _validate_region (ISO 3166-1 alpha-2)"
```

---

## Task 3: `_load_region` + `Config.region` field

**Files:**
- Modify: `src/likesurgeon/config.py` (add `_load_region`, `region` field on `Config`, update `Config.load`)
- Modify: `tests/test_config.py` (append 8 loader tests)

- [ ] **Step 1: Write failing tests for `_load_region`**

The new tests need `json` and `Path`; add them to the existing `from __future__ import annotations` block in `tests/test_config.py`. The post-add header should look like:

```python
"""Tests for config.py — region validation and config.json loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
```

Then append to `tests/test_config.py`:

```python
def _write_config(app_dir: Path, payload: object) -> None:
    """Helper: write `payload` to <app_dir>/config.json, creating dir."""
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "config.json").write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )


def test_load_region_returns_none_when_config_missing(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    assert _load_region(tmp_path) is None


def test_load_region_reads_region_from_config_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, {"region": "KR"})
    assert _load_region(tmp_path) == "KR"


def test_load_region_normalizes_case(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, {"region": "kr"})
    assert _load_region(tmp_path) == "KR"


def test_load_region_returns_none_for_malformed_json(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, "{not valid json")
    assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_non_dict_payload(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    _write_config(tmp_path, ["KR"])
    assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_missing_or_null_or_non_string_key(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    for payload in ({}, {"region": None}, {"region": 42}):
        _write_config(tmp_path, payload)
        assert _load_region(tmp_path) is None


def test_load_region_returns_none_for_empty_string_key(tmp_path: Path) -> None:
    from likesurgeon.config import _load_region

    for payload in ({"region": ""}, {"region": "   "}):
        _write_config(tmp_path, payload)
        assert _load_region(tmp_path) is None


def test_load_region_raises_for_invalid_alpha_2(tmp_path: Path) -> None:
    from likesurgeon.config import InvalidRegionError, _load_region

    _write_config(tmp_path, {"region": "KOREA"})
    with pytest.raises(InvalidRegionError):
        _load_region(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config.py -v -k "load_region"
```

Expected: FAIL with `ImportError: cannot import name '_load_region'`.

- [ ] **Step 3: Implement `_load_region` and add `region` field to `Config`**

Edit `src/likesurgeon/config.py`. Add `import json` to the existing import block at the top (sibling to `import os` / `import re`). Then, after `_validate_region` (which Task 2 just added), insert:

```python
def _load_region(app_dir: Path) -> str | None:
    """Read ``region`` from ``app_dir/config.json``.

    Returns None for: missing file, malformed JSON, non-dict payload, or
    missing/null/non-string ``region`` key. These are "no region
    configured" cases — fall through to the unset-region warning at the
    CLI layer.

    Raises ``InvalidRegionError`` when the key IS present and IS a
    non-empty string but does NOT match ``^[A-Z]{2}$`` — that's a config
    typo, not "unset", so we fail-fast rather than silently disabling
    region detection.
    """
    cfg_path = app_dir / CONFIG_FILENAME
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    region = data.get("region")
    if region is None:
        return None
    if not isinstance(region, str) or not region.strip():
        return None
    return _validate_region(region)
```

Then update the `Config` dataclass — add the `region` field and have `load` populate it. Replace the existing `Config` class body with:

```python
@dataclass(frozen=True)
class Config:
    """Resolved file-system layout for this install."""

    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path
    region: str | None  # ISO 3166-1 alpha-2, or None when not configured

    @classmethod
    def load(cls) -> Config:
        env = os.environ.get(ENV_HOME)
        app_dir = Path(env).expanduser() if env else Path.home() / DEFAULT_APP_DIR_NAME
        return cls(
            app_dir=app_dir,
            db_path=app_dir / DEFAULT_DB_FILENAME,
            ytmusic_browser_path=app_dir / YTMUSIC_BROWSER_FILENAME,
            youtube_oauth_client_path=app_dir / YOUTUBE_OAUTH_CLIENT_FILENAME,
            youtube_token_path=app_dir / YOUTUBE_TOKEN_FILENAME,
            region=_load_region(app_dir),
        )

    def ensure_app_dir(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v -k "load_region"
```

Expected: 8 PASS.

- [ ] **Step 5: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green. (Existing tests that construct `Config(...)` directly do not exist in this repo; all callers go through `Config.load()`.)

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/config.py tests/test_config.py
git commit -m "feat(config): load region from ~/.like-surgeon/config.json with fail-fast on bad alpha-2"
```

---

## Task 4: Region-aware `_classify_status`, `_videos_list` part, and `fetch_video_statuses` threading

**Files:**
- Modify: `src/likesurgeon/youtube_client.py:95-103` (signature of `_classify_status` + region branch), `:306-318` (`_videos_list` part), `:fetch_video_statuses` body
- Modify: `tests/test_youtube_client.py` (append 6 region tests)

This task lands the API surface change atomically: `_classify_status` gets two new params, `_videos_list` adds `contentDetails` to its `part=` query, and `fetch_video_statuses` threads `user_region` from caller down to `_classify_status`. Existing tests stay green because `user_region=None` skips the region branch and `content_details = {}` on legacy fakes goes through the same `or {}` fallback.

- [ ] **Step 1: Write failing tests for region classification**

Append to `tests/test_youtube_client.py`:

```python
def test_classify_status_region_blocked_in_blocked_list() -> None:
    """user_region appears in regionRestriction.blocked → region_blocked."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR", "JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="region_blocked"
    )


def test_classify_status_region_blocked_via_allowed_whitelist() -> None:
    """allowed list is a whitelist — user_region not in it → region_blocked."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"allowed": ["JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="region_blocked"
    )


def test_classify_status_region_allowed_when_in_allowed_list() -> None:
    """user_region in allowed list → available."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"allowed": ["KR", "JP"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=True, reason=None
    )


def test_classify_status_no_region_skips_check() -> None:
    """user_region=None preserves 0.3 behavior — region restriction ignored."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "processed", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR"]}}
    assert _classify_status(status, content, None) == VideoStatus(
        is_available=True, reason=None
    )


def test_classify_status_deleted_takes_precedence_over_region() -> None:
    """uploadStatus=deleted is absolute — region check never runs."""
    from likesurgeon.youtube_client import VideoStatus, _classify_status

    status = {"uploadStatus": "deleted", "privacyStatus": "public"}
    content = {"regionRestriction": {"blocked": ["KR"]}}
    assert _classify_status(status, content, "KR") == VideoStatus(
        is_available=False, reason="deleted"
    )


def test_fetch_video_statuses_passes_region_through(monkeypatch) -> None:
    """fetch_video_statuses receives user_region kwarg and threads it to
    classification — KR blocked items get region_blocked, JP-allowed get
    available, in the same response."""
    from likesurgeon.youtube_client import VideoStatus, YouTubeClient

    client = YouTubeClient(client_secrets_path=None, token_path=None)

    def fake(*, ids: list[str]) -> dict:
        return {"items": [
            {
                "id": "v_kr_blocked",
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "contentDetails": {"regionRestriction": {"blocked": ["KR"]}},
            },
            {
                "id": "v_kr_allowed",
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "contentDetails": {"regionRestriction": {"allowed": ["KR"]}},
            },
        ]}

    monkeypatch.setattr(client, "_videos_list", fake)
    monkeypatch.setattr(client, "_RETRY_SLEEPS", (0, 0))

    out = client.fetch_video_statuses(
        ["v_kr_blocked", "v_kr_allowed"], user_region="KR"
    )
    assert out["v_kr_blocked"] == VideoStatus(
        is_available=False, reason="region_blocked"
    )
    assert out["v_kr_allowed"] == VideoStatus(is_available=True, reason=None)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_youtube_client.py -v -k "classify_status or fetch_video_statuses_passes_region"
```

Expected: FAIL — old `_classify_status(status)` signature errors with `TypeError: _classify_status() takes 1 positional argument but 3 were given`, and `fetch_video_statuses` doesn't accept `user_region`.

- [ ] **Step 3: Update `_classify_status` signature and add region branch**

In `src/likesurgeon/youtube_client.py`, locate `_classify_status` (around line 95) and replace the entire function body with:

```python
def _classify_status(
    status: dict[str, Any],
    content_details: dict[str, Any],
    user_region: str | None,
) -> VideoStatus:
    """Stage-1 status mapping for a present video resource.

    Region check runs only when ``user_region`` is provided; otherwise
    the function preserves 0.3 behavior (status-only). ``uploadStatus``
    'rejected'/'deleted' overrides region restriction because those are
    absolute, not user-region-relative. privacyStatus is deliberately
    NOT consulted: a returned private video means the caller is the
    owner (or has explicit access), so it's playable.
    """
    upload = status.get("uploadStatus")
    if upload == "rejected":
        return VideoStatus(is_available=False, reason="rejected")
    if upload == "deleted":
        return VideoStatus(is_available=False, reason="deleted")
    if user_region:
        rr = content_details.get("regionRestriction") or {}
        blocked = rr.get("blocked") or []
        allowed = rr.get("allowed") or []
        if user_region in blocked:
            return VideoStatus(is_available=False, reason="region_blocked")
        if allowed and user_region not in allowed:
            return VideoStatus(is_available=False, reason="region_blocked")
    return VideoStatus(is_available=True, reason=None)
```

- [ ] **Step 4: Update `_videos_list` to fetch `contentDetails`**

In the same file, locate `_videos_list` (around line 306) and change the `.list(part=...)` argument:

Find this line:
```python
            .list(part="status", id=",".join(ids))
```

Replace with:
```python
            .list(part="status,contentDetails", id=",".join(ids))
```

The docstring already notes `maxResults` is intentionally omitted; no other changes needed.

- [ ] **Step 5: Update `fetch_video_statuses` to accept and thread `user_region`**

In the same file, locate `fetch_video_statuses` (search for `def fetch_video_statuses`). Update its signature and the `_classify_status` call:

Find:
```python
    def fetch_video_statuses(self, video_ids: list[str]) -> dict[str, VideoStatus]:
```

Replace with:
```python
    def fetch_video_statuses(
        self, video_ids: list[str], *, user_region: str | None = None
    ) -> dict[str, VideoStatus]:
```

Find the success-path classification line (inside the loop):
```python
                out[vid] = _classify_status(item.get("status") or {})
```

Replace with:
```python
                out[vid] = _classify_status(
                    item.get("status") or {},
                    item.get("contentDetails") or {},
                    user_region,
                )
```

(The retry/quota/404/failed paths and the `missing_from_videos_list` placeholder logic stay unchanged — region check only applies to the success-path success-item branch.)

- [ ] **Step 6: Run new tests to verify they pass**

```bash
uv run pytest tests/test_youtube_client.py -v -k "classify_status or fetch_video_statuses_passes_region"
```

Expected: 6 PASS.

- [ ] **Step 7: Run full youtube_client test suite to verify no regressions**

```bash
uv run pytest tests/test_youtube_client.py -v
```

Expected: all PASS — including the existing 0.3-era tests that don't pass `contentDetails` or `user_region`. Their fake responses go through `or {}` and `user_region=None`, so the region branch is skipped.

- [ ] **Step 8: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/likesurgeon/youtube_client.py tests/test_youtube_client.py
git commit -m "feat(youtube): region-aware ghost classification (videos.list adds contentDetails, classify accepts user_region)"
```

---

## Task 5: `_resolve_region` precedence helper

**Files:**
- Modify: `src/likesurgeon/cli.py` (add module-level helper)
- Create: `tests/test_cli.py`

This is a tiny pure helper extracted so the `CLI flag > config > None` precedence is unit-testable without spinning up Typer.

- [ ] **Step 1: Write failing tests for `_resolve_region`**

Create `tests/test_cli.py`:

```python
"""Tests for cli.py — region resolution helper, --region flag wiring,
and the InvalidRegionError → _fail conversion in _bootstrap.

Each scan-wiring test uses Typer's CliRunner against a tmp_path home
(LIKE_SURGEON_HOME env override) and a FakeYouTubeClient that captures
the user_region keyword reaching fetch_video_statuses.
"""

from __future__ import annotations


def test_resolve_region_prefers_cli_over_config() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region("JP", "KR") == "JP"


def test_resolve_region_falls_back_to_config_when_cli_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, "KR") == "KR"


def test_resolve_region_returns_none_when_both_none() -> None:
    from likesurgeon.cli import _resolve_region

    assert _resolve_region(None, None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cli.py -v -k "resolve_region"
```

Expected: FAIL with `ImportError: cannot import name '_resolve_region'`.

- [ ] **Step 3: Implement `_resolve_region`**

In `src/likesurgeon/cli.py`, add the helper at module scope. Place it near other private helpers like `_fail` (currently around line 59). Insert after `_fail`:

```python
def _resolve_region(cli_region: str | None, config_region: str | None) -> str | None:
    """Pick the region to use for this scan.

    Precedence: CLI flag > config.json > None. Both inputs are
    pre-validated upstream (Config.load and the Typer parser callback
    both call ``_validate_region`` and raise ``InvalidRegionError`` on
    bad shape), so this helper sees only ``None`` or a valid alpha-2
    code.
    """
    return cli_region or config_region
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_cli.py -v -k "resolve_region"
```

Expected: 3 PASS.

- [ ] **Step 5: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/cli.py tests/test_cli.py
git commit -m "feat(cli): add _resolve_region precedence helper (CLI > config > None)"
```

---

## Task 6: `--region` CLI flag + `_parse_region_flag` callback + scan_youtube_likes wiring

**Files:**
- Modify: `src/likesurgeon/cli.py` (add `_parse_region_flag`, update `scan_youtube_likes`)
- Modify: `tests/test_cli.py` (extend top-of-file imports + append 4 wiring tests at the bottom)

- [ ] **Step 1: Write failing tests for the wiring**

This step splits the new content across two locations in `tests/test_cli.py`:

- **Top of file** — extend the existing import block with the new imports below.
- **Bottom of file** — append the `_FakeYouTubeClient` class, fixtures, and 4 new tests after the existing `test_resolve_region_*` functions Task 5 added.

(All module-level imports must remain grouped at the top; if they end up below test functions ruff will fail with `E402`.)

First, replace the top of `tests/test_cli.py` (everything from the docstring through `from __future__ import annotations`) with this expanded header:

```python
"""Tests for cli.py — region resolution helper, --region flag wiring,
and the InvalidRegionError → _fail conversion in _bootstrap.

Each scan-wiring test uses Typer's CliRunner against a tmp_path home
(LIKE_SURGEON_HOME env override) and a FakeYouTubeClient that captures
the user_region keyword reaching fetch_video_statuses.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

# `youtube_client` carries the symbol we monkeypatch (the
# scan_youtube_likes function does `from .youtube_client import
# YouTubeClient` at call time, which resolves through this module).
import likesurgeon.youtube_client as _yt_mod
from likesurgeon.youtube_client import VideoStatus
```

Then append the fixtures, fake, and tests at the **bottom** of `tests/test_cli.py` (after the three `test_resolve_region_*` functions Task 5 added):

```python
class _FakeYouTubeClient:
    """Replaces YouTubeClient for CLI wiring tests. Records the
    user_region kwarg passed to fetch_video_statuses for assertions."""

    # Class-level recording state. The sentinel string distinguishes
    # "fetch_video_statuses was called with user_region=None" (legit)
    # from "fetch_video_statuses was never called" (test setup error).
    # The `_reset_fake_client` autouse fixture rewrites these before
    # each test.
    last_user_region: Any = "<unset-sentinel>"
    fetch_called: bool = False

    def __init__(self, **kwargs: Any) -> None:
        # Accept and ignore client_secrets_path / token_path / etc.
        pass

    def fetch_liked_videos(self, *, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "snippet": {"title": "Song", "channelTitle": "C", "resourceId": {"videoId": "v1"}},
                "contentDetails": {"videoId": "v1"},
            }
        ]

    def fetch_video_statuses(
        self, video_ids: list[str], *, user_region: str | None = None
    ) -> dict[str, VideoStatus]:
        type(self).last_user_region = user_region
        type(self).fetch_called = True
        return {vid: VideoStatus(is_available=True, reason=None) for vid in video_ids}


@pytest.fixture(autouse=True)
def _reset_fake_client() -> Iterable[None]:
    """Reset class-level recording state between tests."""
    _FakeYouTubeClient.last_user_region = "<unset-sentinel>"
    _FakeYouTubeClient.fetch_called = False
    yield


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LIKE_SURGEON_HOME at tmp_path so Config.load reads a
    test-controlled directory. Required for every CLI wiring test."""
    monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def patch_youtube_client(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYouTubeClient]:
    """Replace likesurgeon.youtube_client.YouTubeClient with the fake
    so the function-local `from .youtube_client import YouTubeClient`
    inside scan_youtube_likes resolves to it."""
    monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeClient)
    return _FakeYouTubeClient


def test_scan_youtube_likes_passes_cli_region_to_fetch_video_statuses(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes", "--region", "JP"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.fetch_called is True
    assert patch_youtube_client.last_user_region == "JP"


def test_scan_youtube_likes_passes_config_region_when_no_cli_flag(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    import json

    (fake_home / "config.json").write_text(json.dumps({"region": "KR"}))

    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.last_user_region == "KR"


def test_scan_youtube_likes_warns_once_when_region_unset(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes"])

    assert result.exit_code == 0, result.output
    assert patch_youtube_client.last_user_region is None
    # Warning text must be present, exactly once.
    assert result.output.count("No region configured") == 1


def test_scan_youtube_likes_rejects_invalid_region_flag(
    fake_home: Path,
    patch_youtube_client: type[_FakeYouTubeClient],
) -> None:
    from likesurgeon.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["scan", "youtube-likes", "--region", "KOREA"])

    assert result.exit_code == 2
    assert "ISO 3166-1 alpha-2" in result.output
    assert patch_youtube_client.fetch_called is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cli.py -v -k "scan_youtube_likes"
```

Expected: FAIL — `--region` flag doesn't exist yet, `_parse_region_flag` not defined, warning text not present.

- [ ] **Step 3: Add `_parse_region_flag` callback**

In `src/likesurgeon/cli.py`, near `_resolve_region` (added in Task 5), add:

```python
def _parse_region_flag(value: str | None) -> str | None:
    """Typer callback for ``--region``: validates the alpha-2 shape and
    raises ``typer.BadParameter`` (exit 2) on bad input so the failure
    surfaces before any API call."""
    if value is None:
        return None
    try:
        return _validate_region(value)
    except InvalidRegionError as e:
        raise typer.BadParameter(str(e)) from e
```

- [ ] **Step 4: Add the `--region` flag and wiring to `scan_youtube_likes`**

In the same file, locate the `scan_youtube_likes` command (search for `def scan_youtube_likes`). It is registered under the `scan` sub-app as `@scan_app.command("youtube-likes")` — preserve that exact decorator (changing it would move the command to a new path and break every `likesurgeon scan youtube-likes` invocation, including the Task 6 CliRunner tests). Update only the signature parameters and body. Replace the existing function with:

```python
@scan_app.command("youtube-likes")
def scan_youtube_likes(
    limit: Annotated[int, typer.Option(help="Maximum number of liked videos to fetch.")] = 5000,
    region: Annotated[
        str | None,
        typer.Option(
            "--region",
            callback=_parse_region_flag,
            help=(
                "ISO 3166-1 alpha-2 country code for region-block detection. "
                "Overrides config.json for this scan only; never written to disk."
            ),
        ),
    ] = None,
) -> None:
    """Fetch YouTube liked videos (LL playlist) and store a snapshot.

    Also runs a per-video availability check (`videos.list?part=status,contentDetails`)
    and persists the result as `SnapshotItem.is_available` /
    `unavailable_reason` so `compare-likes` can surface ghost videos.
    Region-aware detection runs when ``region`` is provided either via
    ``--region`` or ``config.json``.
    """
    from .youtube_client import (
        AuthorizationRequiredError,
        ClientSecretsMissingError,
        YouTubeClient,
        attach_video_statuses,
    )

    cfg, factory = _bootstrap()
    user_region = _resolve_region(region, cfg.region)
    if user_region is None:
        console.print(
            "[yellow]⚠[/yellow] No region configured — region-blocked videos won't "
            "be detected as ghosts. Set [cyan]region[/cyan] in "
            "[cyan]~/.like-surgeon/config.json[/cyan] or pass [cyan]--region <ISO-code>[/cyan]."
        )

    client = YouTubeClient(
        client_secrets_path=cfg.youtube_oauth_client_path,
        token_path=cfg.youtube_token_path,
    )
    try:
        items = client.fetch_liked_videos(limit=limit)
    except (ClientSecretsMissingError, AuthorizationRequiredError) as e:
        _fail(str(e), code=2)

    video_ids: list[str] = []
    for it in items:
        snippet = it.get("snippet") or {}
        content = it.get("contentDetails") or {}
        vid = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if vid:
            video_ids.append(vid)

    statuses = client.fetch_video_statuses(video_ids, user_region=user_region)
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

- [ ] **Step 5: Add the imports `_parse_region_flag` needs**

At the top of `src/likesurgeon/cli.py`, find the existing `from .config import Config` import line and replace it with:

```python
from .config import Config, InvalidRegionError, _validate_region
```

(`_validate_region` is needed by `_parse_region_flag`. `InvalidRegionError` is needed by `_parse_region_flag` and by Task 7's `_bootstrap` catch.)

- [ ] **Step 6: Run wiring tests to verify they pass**

```bash
uv run pytest tests/test_cli.py -v -k "scan_youtube_likes"
```

Expected: 4 PASS.

- [ ] **Step 7: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/likesurgeon/cli.py tests/test_cli.py
git commit -m "feat(cli): add --region flag and wire user_region into scan youtube-likes"
```

---

## Task 7: `_bootstrap()` catches `InvalidRegionError`

**Files:**
- Modify: `src/likesurgeon/cli.py:50-56` (`_bootstrap`)
- Modify: `tests/test_cli.py` (append 2 tests)

- [ ] **Step 1: Write failing tests for the bootstrap convert + Config.load propagation**

Append to `tests/test_cli.py`:

```python
def test_config_load_propagates_invalid_region_in_json(
    fake_home: Path,
) -> None:
    """Unit-level guard: Config.load() raises InvalidRegionError when
    config.json holds a non-alpha-2 region. Pinned independently of the
    CLI catch so a future config refactor can't silently swallow it."""
    import json

    from likesurgeon.config import Config, InvalidRegionError

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    with pytest.raises(InvalidRegionError):
        Config.load()


def test_bootstrap_converts_invalid_region_to_fail(
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_bootstrap catches InvalidRegionError from Config.load and
    converts it to typer.Exit(code=2) with a friendly message — the
    user must never see a traceback for a config typo. The error
    message must surface the offending value so the user can fix it."""
    import json

    import typer

    from likesurgeon.cli import _bootstrap

    (fake_home / "config.json").write_text(json.dumps({"region": "KOREA"}))

    with pytest.raises(typer.Exit) as exc_info:
        _bootstrap()
    assert exc_info.value.exit_code == 2
    # `_fail` prints to err_console (stderr by default in this codebase).
    # Capture both streams so we don't depend on Rich's stream choice.
    out, err = capsys.readouterr()
    combined = out + err
    assert "KOREA" in combined
    assert "ISO 3166-1 alpha-2" in combined
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cli.py -v -k "config_load_propagates or bootstrap_converts"
```

Expected: 1 PASS (`test_config_load_propagates_invalid_region_in_json` — already works since Task 3 made `Config.load` raise) and 1 FAIL (`test_bootstrap_converts_invalid_region_to_fail` — `_bootstrap` doesn't catch yet, so `InvalidRegionError` propagates to the test instead of `typer.Exit`).

- [ ] **Step 3: Wrap `Config.load()` in `_bootstrap`**

In `src/likesurgeon/cli.py`, locate `_bootstrap` (around line 50) and replace the function body. The current body is:

```python
def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory."""
    cfg = Config.load()
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)
```

Replace with:

```python
def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory.

    InvalidRegionError raised by ``Config.load`` (when ``config.json``
    holds a non-alpha-2 region value) is caught here and converted to a
    friendly ``_fail(code=2)`` so the user never sees a traceback for a
    config typo. CLI flag validation lives elsewhere in the Typer
    callback ``_parse_region_flag``.
    """
    try:
        cfg = Config.load()
    except InvalidRegionError as e:
        _fail(str(e), code=2)
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)
```

(The `InvalidRegionError` import was added in Task 6 Step 5.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_cli.py -v -k "config_load_propagates or bootstrap_converts"
```

Expected: 2 PASS.

- [ ] **Step 5: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/likesurgeon/cli.py tests/test_cli.py
git commit -m "feat(cli): _bootstrap catches InvalidRegionError and exits 2 with friendly message"
```

---

## Task 8: README updates + version bump

**Files:**
- Modify: `README.md` (status line, "What it does today", roadmap, local layout, config setup snippet, caveat quota note)
- Modify: `pyproject.toml` (version)
- Modify: `src/likesurgeon/__init__.py` (`__version__`)

- [ ] **Step 1: Bump status line**

In `README.md`, find:
```
**Status:** MVP 0.3 — read-only scanner across YouTube Music *and* YouTube, with cross-source diagnosis, ghost detection, and metadata drift. Local-first. No server, no destructive actions.
```

Replace with:
```
**Status:** MVP 0.3.1 — read-only scanner across YouTube Music *and* YouTube, with cross-source diagnosis, region-aware ghost detection, and metadata drift. Local-first. No server, no destructive actions.
```

- [ ] **Step 2: Update the "What it does today" ghost bullet**

In `README.md`, find the existing ghost bullet:
```
- Detects ghost YouTube likes (deleted, made private, unavailable) at scan time, and metadata drift (title or artists list changes) between snapshots — both surface through `compare-likes` and `issues`.
```

Replace with:
```
- Detects ghost YouTube likes (deleted, made private, region-blocked, unavailable) at scan time, and metadata drift (title or artists list changes) between snapshots — both surface through `compare-likes` and `issues`. Region-blocked detection requires setting an [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) country code (e.g. `KR`) in `~/.like-surgeon/config.json`.
```

- [ ] **Step 3: Add 0.3.1 roadmap row**

In `README.md`, find the Roadmap table. After the existing `**0.3**` row, insert a new row:

```
| **0.3.1**   | Region-aware ghost detection: `videos.list?part=status,contentDetails` checks `regionRestriction` against the user's configured ISO 3166-1 alpha-2 region (`config.json` or `--region` flag). Region-blocked videos surface as `unavailable_video` findings with `unavailable_reason="region_blocked"`. No new commands, quota cost unchanged. |
```

- [ ] **Step 4: Add `config.json` to Local layout**

In `README.md`, find the "Local layout" code block:

```
~/.like-surgeon/
├── like-surgeon.sqlite        # all snapshots, tracks, diagnoses
├── browser.json               # ytmusicapi browser-header auth
├── youtube-oauth-client.json  # downloaded Google OAuth client (Desktop app)
└── youtube-token.json         # persisted refresh token (created on first auth)
```

Replace with:

```
~/.like-surgeon/
├── like-surgeon.sqlite        # all snapshots, tracks, diagnoses
├── browser.json               # ytmusicapi browser-header auth
├── youtube-oauth-client.json  # downloaded Google OAuth client (Desktop app)
├── youtube-token.json         # persisted refresh token (created on first auth)
└── config.json                # optional user settings (e.g. {"region": "KR"})
```

- [ ] **Step 5: Add a setup snippet near the scan instructions**

In `README.md`, find the section showing scan commands (search for `uv run likesurgeon scan ytmusic`). Right before the first scan example, insert a short subsection:

```markdown
**Optional but recommended: configure your region.** Region-blocked videos only get classified as ghosts when likesurgeon knows your region. Create `~/.like-surgeon/config.json`:

```json
{
  "region": "KR"
}
```

Use the [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) code for your country. Without this, `scan youtube-likes` prints a one-time warning and falls back to status-only ghost detection (the 0.3 behavior).

```

(The `\`\`\`json ... \`\`\`` fenced block is part of the inserted README content. The wrapper here just shows it.)

- [ ] **Step 5b: Update the caveat quota note to reflect the new `part=` selection**

In `README.md`, find the existing caveat line (around line 134):

```
- **YouTube Data API quota.** A scan of 5000 likes is ~200 quota units (≈100 `playlistItems.list` + ≈100 `videos.list?part=status` for ghost detection); the default daily quota is 10000. Re-scanning a few times a day is fine.
```

Replace with:

```
- **YouTube Data API quota.** A scan of 5000 likes is ~200 quota units (≈100 `playlistItems.list` + ≈100 `videos.list?part=status,contentDetails` for ghost detection — `contentDetails` adds the `regionRestriction` field needed in 0.3.1, but `videos.list` is 1 unit/call regardless of `part=` selection); the default daily quota is 10000. Re-scanning a few times a day is fine.
```

- [ ] **Step 6: Bump version in `pyproject.toml`**

In `pyproject.toml`, find:
```toml
version = "0.3.0"
```

Replace with:
```toml
version = "0.3.1"
```

- [ ] **Step 7: Bump `__version__` in `src/likesurgeon/__init__.py`**

Replace the file content:

```python
"""like-surgeon: sync, backup, and repair your YouTube Music liked songs."""

__version__ = "0.3.1"
```

- [ ] **Step 8: Resync uv.lock**

```bash
uv sync --extra dev
```

(The version bump invalidates `uv.lock`; `uv sync` regenerates the editable install metadata.)

- [ ] **Step 9: Run full suite + lint**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add README.md pyproject.toml src/likesurgeon/__init__.py uv.lock
git commit -m "docs+chore: README + version bump for 0.3.1"
```

---

## Task 9: Final verification + manual e2e

This task verifies the whole branch end-to-end on the maintainer's real account before opening the PR. It is human-driven; the checks confirm the spec's acceptance criteria.

- [ ] **Step 1: Test suite green from scratch**

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 2: Verify the `--region` flag rejects bogus input before any network call**

```bash
uv run likesurgeon scan youtube-likes --region KOREA
```

Expected: exit code 2, message includes "ISO 3166-1 alpha-2", no scan performed (no snapshot row created, no API call).

- [ ] **Step 3: Verify the unset-region warning fires once when no region is configured**

We don't touch the maintainer's real `~/.like-surgeon/`. Instead, point `LIKE_SURGEON_HOME` at a fresh tmp dir for this single invocation — no config.json there means region is unset. The scan will fail at auth (no credentials in the tmp dir) but the warning is printed before that failure, so the `grep` still counts it. `|| true` swallows the non-zero exit from `_fail`.

```bash
SCRATCH=$(mktemp -d)
LIKE_SURGEON_HOME="$SCRATCH" uv run likesurgeon scan youtube-likes 2>&1 | grep -c "No region configured" || true
rm -rf "$SCRATCH"
```

Expected: prints `1` (warning appears exactly once before the auth failure aborts the scan). If it prints `0`, the warning isn't wiring through. If it prints `2+`, the warning is being emitted per-video instead of once per scan.

- [ ] **Step 4: Verify region-blocked videos now classify as ghosts**

Use the `--region KR` one-off flag. It overrides whatever (if anything) is configured for the maintainer and is never written to disk, so the real `~/.like-surgeon/config.json` is left untouched regardless of whether the maintainer already had one.

```bash
uv run likesurgeon scan youtube-likes --region KR
```

Expected: the `unavailable` count in the summary line is HIGHER than what 0.3 was producing for the same account (the maintainer's data showed 16 ghosts under 0.3; with region-aware detection enabled, many of the 105 fuzzy-match cluster videos should reclassify, e.g. `u5trz8pk7rI`).

Check one specific known case:

```bash
uv run sqlite3 ~/.like-surgeon/like-surgeon.sqlite \
  "SELECT video_id, is_available, unavailable_reason FROM snapshot_items WHERE video_id = 'u5trz8pk7rI' ORDER BY snapshot_id DESC LIMIT 1;"
```

Expected: `u5trz8pk7rI|0|region_blocked`.

- [ ] **Step 5: Verify the new finding type flows through `compare-likes` + `issues`**

```bash
uv run likesurgeon scan ytmusic
uv run likesurgeon compare-likes
uv run likesurgeon issues --type unavailable_video --format json | python3 -c "
import json, sys
data = json.load(sys.stdin)
reasons = {}
for it in data['items']:
    r = it['reason']
    reasons[r] = reasons.get(r, 0) + 1
for r, n in sorted(reasons.items()):
    print(f'  {r}: {n}')
"
```

Expected: the breakdown includes a `video unavailable: region_blocked: <count>` entry alongside the existing `deleted` / `private` reasons.

- [ ] **Step 6: Verify quota usage is unchanged**

Inspect logs / Google Cloud Console quota dashboard. The scan should consume the same `videos.list` count it did under 0.3 (1 unit/call regardless of `part=` selection).

- [ ] **Step 7: Push branch and open PR**

```bash
git push -u origin feat/0.3.1-region-aware-ghost
gh pr create --base main --title "feat: 0.3.1 region-aware ghost detection" --body "$(cat <<'EOF'
## Summary

0.3 ghost detection only checked `videos.list?part=status` — `uploadStatus = rejected/deleted` were the only "unavailable" signals. This missed region-restricted videos that report `processed`/`public` to the API but are not playable for the user's region. On the maintainer's account, all 16 0.3 ghosts were `deleted/private`, while the 105 `possible_pointer_drift` cluster contained many region-blocked-globally videos (e.g. `u5trz8pk7rI`) that 0.3 silently classified as available.

0.3.1 closes the gap:

- `videos.list` now fetches `part=status,contentDetails`. Quota cost unchanged (1 unit/call regardless of `part`).
- New `unavailable_reason="region_blocked"` introduced when the user's configured region appears in `regionRestriction.blocked` (or is missing from a non-empty `regionRestriction.allowed` whitelist).
- New `~/.like-surgeon/config.json` user-config file holds the region (`{"region": "KR"}`). Strict ISO 3166-1 alpha-2 validation; invalid values fail-fast with exit 2 (config-time `InvalidRegionError` caught in `_bootstrap`, CLI flag rejected by Typer callback).
- New `--region KR` CLI override flag for one-off scans (never persisted).
- `_classify_status` order: `rejected` → `deleted` → `region_blocked` → `available`. uploadStatus is absolute and overrides region.

**Carried in the same PR (not part of acceptance):** `issues` table/JSON now surfaces `Source VID` / `Related VID` columns — pre-existing UX improvement from the 0.3 working tree.

Per-task commits follow the [implementation plan](docs/superpowers/plans/2026-05-09-likesurgeon-0.3.1-region-aware-ghost.md). Design rationale (why fail-fast on invalid alpha-2, why `--region` is non-persistent, why the catch lives in `_bootstrap`) is in [docs/superpowers/specs/2026-05-09-likesurgeon-0.3.1-region-aware-ghost-design.md](docs/superpowers/specs/2026-05-09-likesurgeon-0.3.1-region-aware-ghost-design.md).

## Test plan

- [x] All unit + integration tests green (`uv run pytest -q`).
- [x] `ruff check` + `ruff format --check` clean.
- [x] Manual e2e on the maintainer's KR account: `u5trz8pk7rI` reclassified to `region_blocked`; `compare-likes` and `issues --type unavailable_video` show the new reason; one-time warning fires when region is unset; `--region KOREA` exits 2 with friendly message.
- [x] Pre-existing 0.3 tests for `_classify_status` and `fetch_video_statuses` still pass — `user_region=None` preserves 0.3 behavior.
EOF
)"
```

(If the `gh pr create` push fails because the branch already exists upstream from an earlier dry run, drop `-u` and the push command and run `gh pr create ...` directly.)

---

## Self-review checklist (before marking the plan complete)

The plan author runs this against the spec; not a separate task.

- **Spec coverage:**
  - `videos.list?part=status,contentDetails` change → Task 4 Step 4 ✓
  - `_classify_status(status, content_details, user_region)` signature → Task 4 Step 3 ✓
  - `regionRestriction.blocked` / `allowed` semantics → Task 4 (tests + impl) ✓
  - `uploadStatus` precedence over region → Task 4 (`test_classify_status_deleted_takes_precedence_over_region`) ✓
  - `region_blocked` reason value → Task 4 Step 3 ✓
  - `Config.region` + `_load_region` from `config.json` → Task 3 ✓
  - `_validate_region` + `InvalidRegionError` → Task 2 ✓
  - Strict `^[A-Z]{2}$` regex → Task 2 ✓
  - `--region` flag (one-off, no disk write) → Task 6 ✓
  - `_resolve_region` precedence helper → Task 5 ✓
  - `_parse_region_flag` Typer callback (rejects invalid) → Task 6 ✓
  - Unset-region warning (once per scan) → Task 6 (test_warns_once + impl) ✓
  - `_bootstrap` catches `InvalidRegionError` → Task 7 ✓
  - Carried `issues` video_id change → Task 1 ✓
  - README updates (status line, what-it-does, roadmap, local layout, config snippet) → Task 8 ✓
  - Version bump → Task 8 ✓
  - Manual e2e → Task 9 ✓

- **No regressions:**
  - Existing `_classify_status` callers / 0.3 tests → Task 4 Step 7 explicit suite run ✓
  - Existing `fetch_video_statuses` tests → Task 4 Step 7 ✓

- **Type/signature consistency across tasks:**
  - `InvalidRegionError(ValueError)` defined in Task 2 → imported in Task 6 ✓
  - `_validate_region(value: str) -> str` defined in Task 2 → used in Task 3 (`_load_region`) ✓ → used in Task 6 (`_parse_region_flag`) ✓
  - `_load_region(app_dir: Path) -> str | None` defined in Task 3 → called in `Config.load` ✓
  - `Config.region: str | None` defined in Task 3 → read in Task 6 (`_resolve_region(region, cfg.region)`) ✓
  - `_classify_status` 3-arg signature defined in Task 4 → only one caller (`fetch_video_statuses`) updated in same task ✓
  - `fetch_video_statuses(..., *, user_region=None)` defined in Task 4 → called in Task 6 (`scan_youtube_likes`) ✓
  - `_resolve_region(cli, config) -> str | None` defined in Task 5 → used in Task 6 ✓

- **Placeholder scan:** No "TBD", "TODO", "implement later", "similar to Task N", "handle edge cases" verbiage. Each step has either complete code or an exact command. ✓

- **Acceptance vs tests alignment:** Spec acceptance criteria mention 6 + 6 + 8 + 9 = 29 new test cases. The plan covers them across Task 2 (6 validate), Task 3 (8 loader), Task 4 (5 classify + 1 fetch_video_statuses pass-through = 6), Task 5 (3 resolve), Task 6 (4 scan wiring), Task 7 (2 bootstrap/config raise) = **6 + 8 + 6 + 3 + 4 + 2 = 29**. ✓
