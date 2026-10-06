# Development

This document covers the dev environment, test strategy, auth setup details, and operational tips. For system structure and module responsibilities, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Environment

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/zeikar/like-surgeon.git
cd like-surgeon
uv sync --extra dev
```

`uv sync --extra dev` installs both runtime deps (`ytmusicapi`, `typer`, `rich`, `pydantic`, `rapidfuzz`, `sqlalchemy`, `google-auth-oauthlib`, `google-api-python-client`, `browser-cookie3`) and dev deps (`pytest`, `ruff`).

Run tools via `uv run` (no venv activation needed) or `source .venv/bin/activate` once and call them directly.

```bash
uv run likesurgeon --help
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Test strategy

Test layout mirrors source: `tests/test_<module>.py` per `src/likesurgeon/<module>.py`. Three flavors:

- **Unit** — pure-functional modules (`classify`, `normalize`, `drift`, `align`) get exhaustive coverage with lightweight stand-in inputs. No DB, no network. `test_align.py` drives the LL→LM alignment with small synthetic lists (consecutive relinks, duplicate copies, edge gaps, *k* ≠ *m*).
- **Integration** — `test_db`, `test_snapshot`, `test_diagnosis`, `test_pipeline`, `test_sync`, `test_sync_dispatch`, `test_sync_preflight`, `test_doctor`, `test_export`, `test_diff` use the in-memory SQLite fixture from [`tests/conftest.py`](../tests/conftest.py):
  ```python
  @pytest.fixture
  def session() -> Iterator[Session]:
      """In-memory SQLite session, schema initialized."""
  ```
  `diff_snapshots` takes a `Session` and reads snapshot/items rows directly, so it lives with the integration tier rather than the pure modules above.
- **CLI wiring** — `test_cli` uses Typer's `CliRunner` against a `tmp_path` home (`LIKE_SURGEON_HOME` env override) and monkeypatched fakes for `YouTubeClient` / `YTMusicClient`. The pattern: replace the provider class on the module the command imports it from, then assert the captured kwargs.
- **Live** — `test_ytmusic_client_live.py` is opt-in (`@pytest.mark.live`), deselected by default via `pyproject.toml`. Set `LIKESURGEON_LIVE_BROWSER=firefox` to point at a non-default browser:
  ```bash
  uv run pytest -m live
  LIKESURGEON_LIVE_BROWSER=firefox uv run pytest -m live
  ```

`pyproject.toml` sets `addopts = "-q -m 'not live'"` so the default `uv run pytest` skips live tests.

### Fakes and isolation

Three recurring patterns:

- **In-memory DB** — every test that touches SQL uses `session` fixture (clean schema per test). The fixture creates a fresh `:memory:` engine, calls `init_db`, and yields a session.
- **`tests/fake_account.py`** — `FakeAccount` is one fake account seen through both sync clients (the YouTube and YT Music surfaces `sync_dispatch` uses). It holds LL ratings (newest first) plus a render map; LM is derived from LL on every read (each liked video shows as `renders.get(video, video)`, or not at all for `None`), so a write changes LM the way the rendering model says. That is what `test_sync_dispatch.py` and the `sync` tests in `test_cli.py` rely on to exercise the post-unlike LM check and restore. Knobs simulate a like that doesn't land, an unlike that lands late, a re-liked video that renders (`relike_renders`: video → what LM shows once a like on it lands, `None` for not shown), write/read errors, quota exhaustion and stale or failing LM reads.
- **`LIKE_SURGEON_HOME` override** — CLI tests set the env var to `tmp_path` so `Config.load()` reads a test-controlled directory:
  ```python
  @pytest.fixture
  def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
      monkeypatch.setenv("LIKE_SURGEON_HOME", str(tmp_path))
      return tmp_path
  ```

For provider clients, monkeypatch the *symbol the command imports*, not the class definition. E.g. `scan_youtube_likes` does `from .youtube_client import YouTubeClient` at call time, so:

```python
import likesurgeon.youtube_client as _yt_mod
monkeypatch.setattr(_yt_mod, "YouTubeClient", _FakeYouTubeClient)
```

## Auth setup details

Two auth flows live in this project. Both write credentials to `~/.like-surgeon/` with POSIX `0o600`.

### YouTube Music (browser-header)

Cookie-based auth via [browser-cookie3](https://pypi.org/project/browser-cookie3/). The `--from-browser` flag reads cookies straight from a logged-in browser's local store and writes a ytmusicapi-compatible `browser.json`.

```bash
uv run likesurgeon auth ytmusic --from-browser chrome
```

Supported names (lowercase): `chrome`, `chromium`, `firefox`, `edge`, `brave`, `safari`, `opera`, `opera_gx`, `librewolf`, `vivaldi`, `arc`, `w3m`, `lynx`.

**macOS Keychain prompt.** Chromium-family browsers encrypt their cookie store with a Keychain entry; the first run prompts you to allow `python` (or `Terminal`) to read it. Firefox stores cookies in plain SQLite and avoids the prompt. Safari may need **Full Disk Access** (System Settings → Privacy & Security) for the terminal/IDE.

**Manual fallback** (if `--from-browser` doesn't work in your environment):

```bash
uv run likesurgeon auth ytmusic
```

prints the four-step `ytmusicapi browser` paste recipe.

**Why no OAuth.** ytmusicapi 1.12 + Google's current backend reject every non-TV `clientName` for OAuth-issued tokens, and the TV clients return YouTube-shape responses ytmusicapi can't parse. See [`docs/notes/ytmusic-oauth-tvhtml5-fallback.md`](notes/ytmusic-oauth-tvhtml5-fallback.md) for the full investigation. Browser-header is the working path for now.

### YouTube Data API (OAuth)

```bash
uv run likesurgeon auth youtube
```

First run prints a 6-step Google Cloud Console walkthrough that ends with you placing `youtube-oauth-client.json` (Desktop-app OAuth client) in `~/.like-surgeon/`. Subsequent runs open a browser for consent and persist the refresh token to `~/.like-surgeon/youtube-token.json`.

The refresh token rotates silently after that. If you delete `youtube-token.json`, the next command re-runs the consent flow.

### Quota budget

YouTube Data API default daily quota is **10,000 units**. A single full scan of 5,000 likes costs ~200 units:
- ~100 units for `playlistItems.list` (50 items per page, 1 unit/call)
- ~100 units for `videos.list?part=status,contentDetails` (50 video ids per call, 1 unit/call regardless of `part` selection)
- 1 unit for the likes `itemCount` lookup (`playlists.list`; the likes playlist id was already resolved by the fetch) that `scan youtube-likes` runs afterwards

So you can comfortably re-scan dozens of times per day. If you hit the quota, the YouTube client surfaces a clean `quota_exceeded` reason rather than a traceback.

## Database operations

### Schema lifecycle

`init_db(engine)` (called from `_bootstrap`) creates all tables on first run. There is no Alembic-style migration framework, but `make_engine` calls [`_migrate_in_place`](../src/likesurgeon/db.py) on every connection, which idempotently issues `ALTER TABLE ... ADD COLUMN` for nullable columns introduced after a table was first created (e.g. `snapshot_items.is_available` / `unavailable_reason` from 0.3). Existing rows aren't rewritten — the new columns just default to NULL.

This handles **additive** changes only. For breaking schema changes (renamed columns, type changes, FK reshuffles) during the 0.x series, drop the DB and re-scan:

```bash
rm ~/.like-surgeon/like-surgeon.sqlite
uv run likesurgeon scan ytmusic
uv run likesurgeon scan youtube-likes --region KR
```

The DB only holds derived data (snapshots are local archives, diagnoses are local computations). No original-source state is lost — re-scanning rebuilds everything.

### Inspecting the DB

`uv run sqlite3` is your friend for ad-hoc queries:

```bash
# Recent snapshots
uv run sqlite3 ~/.like-surgeon/like-surgeon.sqlite \
  "SELECT id, source, created_at, raw_count FROM snapshots ORDER BY id DESC LIMIT 10;"

# Why was a specific video classified as unavailable?
uv run sqlite3 ~/.like-surgeon/like-surgeon.sqlite \
  "SELECT video_id, is_available, unavailable_reason FROM snapshot_items
   WHERE video_id = 'u5trz8pk7rI' ORDER BY snapshot_id DESC;"

# Latest diagnosis breakdown
uv run sqlite3 ~/.like-surgeon/like-surgeon.sqlite \
  "SELECT issue_type, COUNT(*) FROM diagnosis_items
   WHERE diagnosis_id = (SELECT MAX(id) FROM diagnoses)
   GROUP BY issue_type ORDER BY 2 DESC;"
```

For larger lookups (`Track.id.in_(...)`), the CLI batches into chunks of 500 to stay under SQLite's `SQLITE_MAX_VARIABLE_NUMBER` (default 999 on older builds).

## Debugging tips

**Isolated home for experiments.** Set `LIKE_SURGEON_HOME` to a tmp dir to avoid polluting your real DB while testing:

```bash
SCRATCH=$(mktemp -d)
LIKE_SURGEON_HOME="$SCRATCH" uv run likesurgeon scan youtube-likes --region KR
rm -rf "$SCRATCH"
```

**Tracebacks for unexpected failures.** The CLI catches known exceptions (`InvalidRegionError`, `ClientSecretsMissingError`, `AuthorizationRequiredError`, `CookieExtractionError`, plus `AuthFileMissingError` / `UnexpectedResponseError` from YT Music and `HttpError` from the YouTube liked-videos fetch) and converts them to `_fail(...)`. The YT Music client wraps every ytmusicapi failure (parse, server, transport) into those two types, keeping the original as `__cause__`. Anything else (e.g. sqlalchemy IntegrityError) is intentionally allowed to propagate as a traceback so you have stack frames to debug from.

**JSON output for piping.** `issues --format json` is structured for `jq` / `python3 -c '...'`:

```bash
uv run likesurgeon issues --type relinked --format json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'Total: {len(data[\"items\"])}')
for it in data['items'][:5]:
    print(f'  {it[\"reason\"]}: {it[\"source_video_id\"]}')
"
```

## Workflow

Branch → commit → PR. Per-task TDD discipline (failing test → implementation → green → commit) for non-trivial changes; small fixes (typos, docstring drift, comment cleanup) can land as single commits.

```bash
git checkout -b feat/<short-name>
# ... work ...
uv run pytest -q && uv run ruff check . && uv run ruff format --check .
git push -u origin feat/<short-name>
gh pr create --base main --title "..." --body "..."
```

CI is GitHub Actions ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) running `ruff check`, `ruff format --check`, and `pytest` on Ubuntu / Python 3.13 for every push to main and every PR. Live tests stay deselected (the runner has no logged-in browser). PR descriptions follow the pattern in recent merged PRs (Summary + Test plan).

## Release process

1. Bump version in `pyproject.toml` and `src/likesurgeon/__init__.py` (must match).
2. Run `uv sync --extra dev` to refresh `uv.lock`.
3. Update `README.md` roadmap + relevant caveats.
4. Update [ARCHITECTURE.md](ARCHITECTURE.md) roadmap section if scope-relevant.
5. Tag the merge commit on main: `git tag v<X.Y.Z> && git push --tags`.
6. `gh release create v<X.Y.Z> ...`. Publishing the release runs [`.github/workflows/release.yml`](../.github/workflows/release.yml): it fails unless the tag matches both version strings, builds with `uv build`, runs `twine check --strict`, and uploads to [PyPI](https://pypi.org/project/likesurgeon/) through trusted publishing (no token; the publisher is registered on PyPI for workflow `release.yml`, environment `pypi`).

To check the package locally first: `uv build && uvx twine check --strict dist/*`, then `uvx --from dist/likesurgeon-<X.Y.Z>-py3-none-any.whl likesurgeon --version`.

### README images

The README loads its images from `raw.githubusercontent.com` (absolute URLs, so they also show on PyPI). Sources are the HTML files in [`docs/assets/src/`](assets/src/); edit one, then run `docs/assets/src/render.sh` (Google Chrome + uv) to regenerate the PNGs next to it.
