# likesurgeon 0.3.1 — Region-aware ghost detection

## Goal

0.3 ghost detection currently calls `videos.list?part=status` and treats `uploadStatus = rejected/deleted` as the only "unavailable" signals. This misses **region-restricted** videos: videos whose `status` reports `processed`/`public` (or `unlisted`) yet are not playable for the user because YouTube blocks them for the user's region. Such videos appear as `is_available=true` in 0.3 but are functionally indistinguishable from ghosts to the user.

0.3.1 closes that gap by also fetching `part=contentDetails` and inspecting `regionRestriction.blocked` / `regionRestriction.allowed` against a configured user region. A new `unavailable_reason = "region_blocked"` is introduced.

This is a tightly scoped patch. No new commands, no behavior change for users who don't configure a region (for whom 0.3.1 == 0.3 + a one-time warning).

**Carried in the same PR (not part of 0.3.1 acceptance):** the `issues` command's text/JSON output gains source/related `video_id` fields. Already implemented in the working tree post-0.3 merge; functionally independent of region-aware detection but ships in the same PR to avoid a one-commit standalone release. See "Carried changes" section near the end for the explicit boundary.

---

## Background: why 0.3 misses these

Concrete observation on the maintainer's own data (1257 YT likes, 16 ghosts detected by 0.3):

- All 16 ghosts are `unavailable_reason ∈ {deleted, private}`. None are `region_blocked` because 0.3 doesn't check that signal.
- The 105 `possible_pointer_drift` findings (stage-3 fuzzy matches) all have *live* `is_available = true` sources per 0.3, but spot-checking one of them (`u5trz8pk7rI`) reveals:

```
status:           {uploadStatus: processed, privacyStatus: unlisted, ...}
contentDetails:
  regionRestriction: {blocked: [AD, AE, ..., KR, ..., ZW]}  # 247 countries
```

The video is `unlisted` and globally region-blocked. From the user's perspective in KR it's a ghost; from 0.3's perspective it's available. This explains the entire 0.3 fuzzy-match-but-feels-ghost cluster.

`videos.list` quota cost is 1 unit per call regardless of `part=` selection (per [YouTube API quota docs](https://developers.google.com/youtube/v3/determine_quota_cost)), so adding `contentDetails` is free quota-wise. Response payload grows (the `regionRestriction.blocked` list can be hundreds of entries), but we don't persist it — only the boolean classification.

---

## Out of scope for 0.3.1

- **VPN / IP-based region detection.** The user's *viewing* region (the IP YouTube enforces against) can differ from any configured value. This patch trusts the configured region as source of truth. Mismatches surface as findings the user can ignore.
- **Auto-detection from OAuth account country.** YouTube/Google's `channels.list?mine=true` exposes `snippet.country`, which would let us suggest a default. Decided to keep 0.3.1 minimal — config file with explicit manual entry. Auto-suggest can land in a later patch.
- **Sync / write actions.** 0.4 handles unlike/like; this patch is read-side only.
- **Retroactive migration of existing snapshot rows.** The next scan re-classifies items with the new logic. Existing 0.3 rows keep their (possibly inaccurate) `is_available` until re-scanned. No DB migration code.
- **Persisting `regionRestriction` raw data.** The list can be 200+ entries per video; we don't need it post-classification. `videos.list` responses are never persisted to the DB — the scan command computes a `VideoStatus` and injects only the derived `_likesurgeon_video_status` dict onto the corresponding `playlistItems.list` entry, which `_youtube_to_record` then strips before serialising `raw_json`. The *playlistItems.list* `contentDetails` block (a different `contentDetails`, just a `videoId` carrier) stays in `raw_json` unchanged — it's the existing 0.3 behavior, and `regionRestriction` does not appear there.

---

## Architecture

### Stage-1 mapping (updated)

`_classify_status` in `youtube_client.py` becomes:

```python
def _classify_status(
    status: dict[str, Any],
    content_details: dict[str, Any],
    user_region: str | None,
) -> VideoStatus:
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

Order: `uploadStatus` checks first (those are absolute, region-independent), then region restriction. A `deleted` video that's also region-blocked stays `reason="deleted"` because deletion is the more fundamental state.

If `user_region is None` (config missing, no `--region` flag), region check is skipped entirely. The classifier behaves identically to 0.3 for that user — region is opt-in.

### `videos.list` call

`YouTubeClient._videos_list`:

```python
def _videos_list(self, *, ids: list[str]) -> dict[str, Any]:
    service = self._service()
    return (
        service.videos()
        .list(part="status,contentDetails", id=",".join(ids))
        .execute()
    )
```

Single change: `part="status"` → `part="status,contentDetails"`. Quota unchanged (1 unit/call).

### `fetch_video_statuses` signature

Adds an optional `user_region` parameter, threads through to `_classify_status`:

```python
def fetch_video_statuses(
    self, video_ids: list[str], *, user_region: str | None = None
) -> dict[str, VideoStatus]:
    ...
    out[vid] = _classify_status(
        item.get("status") or {},
        item.get("contentDetails") or {},
        user_region,
    )
```

Existing callers that don't pass `user_region` keep working — they get 0.3 semantics. The CLI scan command is the only production caller and gets updated.

### Config

`Config` class (config.py) gains a `region: str | None` field, loaded from a new `~/.like-surgeon/config.json`. Add `import json` and `import re` at the top of `config.py`:

```python
@dataclass(frozen=True)
class Config:
    app_dir: Path
    db_path: Path
    ytmusic_browser_path: Path
    youtube_oauth_client_path: Path
    youtube_token_path: Path
    region: str | None  # ISO 3166-1 alpha-2 code, or None if not configured

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


_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")


class InvalidRegionError(ValueError):
    """Raised when a region value is not ISO 3166-1 alpha-2 (^[A-Z]{2}$).

    Surfaced fail-fast at config load and CLI flag parse so a typo (e.g.
    `KOREA`, `kr\\nx`, an empty string after trim) can't silently disable
    region detection — that would let a user think they were checking
    region-blocks while every video skipped the check.
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


def _load_region(app_dir: Path) -> str | None:
    """Read `region` from app_dir/config.json.

    Returns None for: missing file, malformed JSON, non-dict payload, or
    missing/null/non-string `region` key (these are "no region configured"
    cases — fall through to the unset-region warning path).

    Raises InvalidRegionError if the key is present and a non-empty string
    but does NOT match ^[A-Z]{2}$ — that's a config typo, not "unset", so
    we fail-fast rather than silently disabling region detection.
    """
    cfg_path = app_dir / "config.json"
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
    return _validate_region(region)  # raises InvalidRegionError on bad value
```

`config.json` shape (this patch):

```json
{
  "region": "KR"
}
```

Future settings live alongside (no migration needed; `_load_region` only reads the `region` key). The file is optional; missing file = no region configured (no warning at load — the scan command emits the warning when region is None).

### CLI override

`scan youtube-likes --region KR` flag overrides whatever config says, **for that one invocation only — the patch never writes to config.json.** Permanent region setting is done by hand-editing `config.json`.

Region precedence is encapsulated in a pure helper so the wiring is unit-testable without spinning up Typer:

```python
def _resolve_region(cli_region: str | None, config_region: str | None) -> str | None:
    """Pick the region to use for this scan.

    Precedence: CLI flag > config.json > None. Both inputs are
    pre-validated (Config.load and the Typer parser both call
    `_validate_region` and raise InvalidRegionError on bad shape), so
    this helper sees only None or a valid alpha-2 code.
    """
    return cli_region or config_region
```

CLI option (Typer parser does shape validation up front via a `callback=`):

```python
def _parse_region_flag(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _validate_region(value)
    except InvalidRegionError as e:
        raise typer.BadParameter(str(e)) from e


@app.command()
def scan_youtube_likes(
    limit: Annotated[int, typer.Option(...)] = 5000,
    region: Annotated[
        str | None,
        typer.Option(
            "--region",
            callback=_parse_region_flag,
            help="ISO 3166-1 alpha-2 country code for region-block detection. Overrides config.json for this scan only; never written to disk.",
        ),
    ] = None,
) -> None:
    cfg, factory = _bootstrap()
    user_region = _resolve_region(region, cfg.region)
    if user_region is None:
        console.print(
            "[yellow]⚠[/yellow] No region configured — region-blocked videos won't be detected as ghosts. "
            "Set `region` in [cyan]~/.like-surgeon/config.json[/cyan] or pass `--region <ISO-code>`."
        )
    ...
    statuses = client.fetch_video_statuses(video_ids, user_region=user_region)
```

The unset-region warning prints once per scan, not per-video. Invalid `--region` value → `typer.BadParameter` (exit code 2) before any API call.

For invalid `region` in `config.json`, the catch lives in `_bootstrap()` — that's the function every command calls before doing anything else, and it's where `Config.load()` is invoked. Wrap the call:

```python
def _bootstrap() -> tuple[Config, sessionmaker]:
    """Resolve config, ensure app dir + schema, return a session factory."""
    try:
        cfg = Config.load()
    except InvalidRegionError as e:
        _fail(str(e), code=2)
    cfg.ensure_app_dir()
    engine = make_engine(cfg.db_path)
    init_db(engine)
    return cfg, make_session_factory(engine)
```

This works for the installed `pyproject.toml` console-script entry (`likesurgeon.cli:app`) and the `__main__` fallback alike — any path that touches a command goes through `_bootstrap()` first. `_fail` already maps to `typer.Exit(code=2)` and prints to `err_console`, matching the existing UX of other config-time errors (e.g. missing OAuth client). A traceback never reaches the user.

---

## Data model

No schema changes. `SnapshotItem.unavailable_reason` is already `VARCHAR(32)`; the new value `"region_blocked"` (14 chars) fits.

---

## Test plan

### Unit tests in `tests/test_youtube_client.py`

- `test_classify_status_region_blocked_in_blocked_list` — `regionRestriction.blocked=["KR"]`, user_region=`"KR"` → `(False, "region_blocked")`.
- `test_classify_status_region_blocked_via_allowed_list` — `regionRestriction.allowed=["JP"]`, user_region=`"KR"` → `(False, "region_blocked")`.
- `test_classify_status_region_allowed_when_in_allowed_list` — `allowed=["KR","JP"]`, user_region=`"KR"` → `(True, None)`.
- `test_classify_status_no_region_skips_check` — `regionRestriction.blocked=["KR"]` but `user_region=None` → `(True, None)` (unchanged from 0.3 behavior for un-configured users).
- `test_classify_status_deleted_takes_precedence_over_region` — `uploadStatus="deleted"` AND `regionRestriction.blocked=["KR"]`, user_region=`"KR"` → `(False, "deleted")`.
- `test_fetch_video_statuses_passes_region_through` — monkeypatch `_videos_list` to return responses with `regionRestriction`, assert classification depends on `user_region` argument.

### Unit tests in `tests/test_config.py` (new file)

Validation helper:

- `test_validate_region_accepts_uppercase_alpha_2` — `"KR"` → `"KR"`.
- `test_validate_region_normalizes_case_and_whitespace` — `"  kr  "` → `"KR"`.
- `test_validate_region_rejects_three_letter_code` — `"KOR"` → `InvalidRegionError`.
- `test_validate_region_rejects_full_country_name` — `"Korea"` → `InvalidRegionError`.
- `test_validate_region_rejects_empty_after_strip` — `"   "` → `InvalidRegionError`.
- `test_validate_region_rejects_embedded_whitespace_or_punct` — `"K R"`, `"K\nR"`, `"K1"` → all `InvalidRegionError`.

Loader (each writes a temp `config.json` and calls `_load_region(tmp_path)`):

- `test_load_region_returns_none_when_config_missing` — no file → `None`.
- `test_load_region_reads_region_from_config_json` — `{"region": "KR"}` → `"KR"`.
- `test_load_region_normalizes_case` — `{"region": "kr"}` → `"KR"`.
- `test_load_region_returns_none_for_malformed_json` — `b"{"` → `None`, no exception.
- `test_load_region_returns_none_for_non_dict_payload` — top-level list or string → `None`.
- `test_load_region_returns_none_for_missing_or_null_or_non_string_key` — `{}`, `{"region": null}`, `{"region": 42}` → all `None`.
- `test_load_region_returns_none_for_empty_string_key` — `{"region": ""}`, `{"region": "   "}` → `None` (treated as "unset", not invalid).
- `test_load_region_raises_for_invalid_alpha_2` — `{"region": "KOREA"}` → `InvalidRegionError` (this is a typo, NOT "unset" — fail-fast).

### Unit tests in `tests/test_cli.py` (new file or appended to existing)

These pin the wiring so a regression that drops the `user_region` argument fails loudly even when classification logic itself is fine.

- `test_resolve_region_prefers_cli_over_config` — `_resolve_region("JP", "KR")` → `"JP"`.
- `test_resolve_region_falls_back_to_config_when_cli_none` — `_resolve_region(None, "KR")` → `"KR"`.
- `test_resolve_region_returns_none_when_both_none` — `_resolve_region(None, None)` → `None`.
- `test_scan_youtube_likes_passes_cli_region_to_fetch_video_statuses` — invoke `scan_youtube_likes` via `typer.testing.CliRunner` with `--region JP` against fakes, assert the `user_region="JP"` keyword reached `fetch_video_statuses`.
- `test_scan_youtube_likes_passes_config_region_when_no_cli_flag` — same harness, no `--region` flag, but `Config.region = "KR"` → `fetch_video_statuses(user_region="KR")`.
- `test_scan_youtube_likes_warns_once_when_region_unset` — both None → exactly one warning printed (substring match), no exception, `fetch_video_statuses(user_region=None)`.
- `test_scan_youtube_likes_rejects_invalid_region_flag` — `--region KOREA` → exit code 2, message mentions "ISO 3166-1 alpha-2", `fetch_video_statuses` never called.
- `test_config_load_propagates_invalid_region_in_json` — write `{"region": "KOREA"}` to the test app dir, `Config.load()` raises `InvalidRegionError`. (Unit-level guard: confirms the load layer doesn't swallow the bad value.)
- `test_bootstrap_converts_invalid_region_to_fail` — point `LIKE_SURGEON_HOME` at a tmp dir whose `config.json` has `{"region": "KOREA"}`, call `_bootstrap()`, assert `typer.Exit` with `exit_code == 2` and the printed error mentions the offending value. (This is the acceptance-pinning test for the `_bootstrap` catch path — without it the `Config.load()` raise could regress to a traceback at the user level.)

### Integration / manual e2e

- Run `scan youtube-likes` with `region=KR` configured against the maintainer's account. Expected: previously-fuzzy-matched-as-live videos like `u5trz8pk7rI` get reclassified as `is_available=False, unavailable_reason="region_blocked"`. The 16 → ghost count rises (likely substantially given the 105 fuzzy-match cluster).
- Re-run `compare-likes`. Expected: `Unavailable videos (ghost)` count in the summary table rises; some `possible_pointer_drift` rows correspondingly disappear from the matched bucket because their source is now ghost (still surfaces under `unavailable_video` finding).
- Run `scan youtube-likes` *without* region configured (rename config.json out of the way). Expected: identical behavior to 0.3 + warning printed once.

---

## CLI changes

```
likesurgeon scan youtube-likes                  # uses config.region
likesurgeon scan youtube-likes --region KR      # one-off override
likesurgeon scan youtube-likes                  # warns if region unset
```

No new commands, no other command changes. `compare-likes`, `issues`, `doctor` consume the new `region_blocked` value transparently (it slots into the existing `unavailable_video` finding type).

---

## Persistence and migration

- No schema changes.
- No data migration. Existing 0.3 snapshot rows keep their pre-region `is_available` value until next scan; users who care re-scan to refresh.
- **`config.json` is never auto-created or written by `likesurgeon`.** The patch does not write to disk. To enable region-aware detection the user creates the file by hand. The `--region` CLI flag is a one-off override for a single scan invocation only — it does not get persisted to `config.json`. (Auto-create / "save flag value" is deferred to a later patch if a UX need surfaces.)

---

## README updates

- Status line: `MVP 0.3 → MVP 0.3.1` (Korean ko: "0.3.1 — region-aware ghost detection").
- New short subsection under "What it does today": region-blocked detection and how to enable (`config.json` snippet).
- Roadmap row addition: `**0.3.1**` describing region-aware ghost detection.
- "Local layout" section: add `config.json` to the file list.
- "Caveats" quota note remains unchanged (`videos.list` cost was already accurate at 1 unit/call).

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| User's *viewing* region (VPN, travel) differs from configured `region` | `--region` flag handles the one-off case (override for that scan only). The unset-region warning makes the *absence* of region detection explicit so a user who forgot to configure won't silently believe the check ran. We do not detect mismatch between configured value and viewing region — out of scope (would require IP-based lookup). |
| `regionRestriction.allowed` semantics misunderstood (allowed = whitelist, not "extra permitted") | Unit test pins both branches |
| Empty `allowed` list ambiguous (some videos return `allowed: []`?) | Code treats `allowed` truthy-only — empty list = no whitelist constraint, falls through to default `(True, None)` |
| Config JSON corrupted by user | `_load_region` returns `None` on any structural failure (missing/malformed JSON, wrong shape) — degrades to "no region configured" + warning, never crashes |
| User typos a non-alpha-2 region (e.g. `"Korea"`, `"KOR"`, `"kr1"`) | Strict `^[A-Z]{2}$` validation in `_validate_region`. CLI flag → `typer.BadParameter` (exit 2) before any API call. `config.json` → `InvalidRegionError` raised at `Config.load`, caught in `_bootstrap()` and converted to `_fail(..., code=2)` with the offending value. Silent disablement is explicitly avoided. |

---

## Carried changes (not part of 0.3.1 acceptance)

The `issues` command UX improvement was implemented in the working tree post-0.3 merge but never shipped because no follow-up release happened. It rides in this PR for release-cadence reasons only — there's no functional dependency on region-aware detection.

What it changes:
- JSON output adds `source_video_id` / `related_video_id` keys (alongside existing `source_track_id` / `related_track_id`).
- Table output replaces `Source TID` / `Related TID` columns (internal SQLite PKs) with `Source VID` / `Related VID` columns (YouTube `video_id`s the user can copy-paste).
- A bulk lookup of referenced `Track` rows is added before the existing session closes; nothing in the diagnosis or matching pipeline changes.

What is NOT done as part of this carry:
- No new tests are added for the carried change beyond what already exists in the working tree at PR-open time. (If existing tests don't cover it, that's a separate follow-up.)
- The README does not call out the carried change; it lands silently.

If during implementation this carry causes any conflict with the region work, **split it into a standalone PR rather than entangle the two** — the carry has zero correctness coupling to 0.3.1.

---

## Acceptance criteria (0.3.1 core only — the carry is judged by "no regression")

- `uv run pytest -q` and `uv run ruff check . && uv run ruff format --check .` clean.
- New unit tests pass:
  - `_classify_status` — 6 region cases (blocked-list match, allowed-list whitelist, allowed-list match, no-region-skip, deleted-precedence, plus the existing 0.3 cases regress-tested).
  - `_validate_region` — 6 validation cases.
  - `_load_region` — 8 config-loading cases (incl. fail-fast on invalid alpha-2 in `config.json`).
  - `_resolve_region` and CLI wiring — 9 cases pinning that `--region` and `config.region` both reach `fetch_video_statuses(user_region=...)`, that the unset warning emits exactly once, that invalid `--region` exits 2 before any network call, and that `_bootstrap()` converts `InvalidRegionError` from `config.json` into `_fail(code=2)` (not a traceback).
- Re-scanning the maintainer's account with `region=KR` configured surfaces `u5trz8pk7rI` (and similar pattern videos) as `is_available=False, unavailable_reason="region_blocked"`.
- Running scan without region configured prints exactly one warning and behaves identically to 0.3 (no behavior regressions on un-configured users).
- Running scan with an invalid `--region` value or invalid `config.json` `region` value exits with code 2 and a message naming the offending value.
- `videos.list` quota usage per scan unchanged (5000 likes ≈ 100 calls, still 1 unit/call).
- README status line updated to 0.3.1, roadmap row added, `config.json` documented in Local layout and a brief setup snippet added.

(The carried `issues --video-id` change is not counted here — it must simply not regress existing tests.)
