# like-surgeon

> Sync, backup, and repair your YouTube Music liked songs.

**Status:** MVP 0.3.1 — read-only scanner across YouTube Music *and* YouTube, with cross-source diagnosis, region-aware ghost detection, and metadata drift. Local-first. No server, no destructive actions.

## What it does today

- Authenticates with YouTube Music (`ytmusicapi`, browser-header) and YouTube Data API v3 (Google OAuth, desktop client).
- Snapshots `ytmusic_liked_songs` (YouTube Music likes) and `youtube_liked_videos` (YouTube LL playlist) into a local SQLite DB.
- Diffs any two snapshots, exports any snapshot to JSON.
- Classifies YouTube liked videos as music-candidate / not via heuristics (channel ends with `- Topic`, "Provided to YouTube by …", "Official Music Video", "Lyric Video", `artist - title`, etc.; negatives like `vlog`, `tutorial`, `gameplay`).
- `compare-likes` does a three-stage match (`video_id` → `canonical_key` → RapidFuzz fuzzy on `title | artists`) and persists findings as a `Diagnosis` plus per-finding `DiagnosisItem` rows.
- Detects ghost YouTube likes (deleted, made private, region-blocked, unavailable) at scan time, and metadata drift (title or artists list changes) between snapshots — both surface through `compare-likes` and `issues`. Region-blocked detection requires setting an [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) country code (e.g. `KR`) in `~/.like-surgeon/config.json`.
- `issues` lists the latest diagnosis with type/confidence filters and JSON output.
- `doctor` is now multi-source: counts per source, latest diagnosis summary, and a match-rate health score.

Snapshots preserve **point-in-time metadata** — the title, channel, description, etc. that the provider returned at scan time are frozen on each `SnapshotItem`. A later rename in YouTube Music or YouTube doesn't rewrite history.

## Roadmap

| Milestone   | Scope                                                                |
|-------------|----------------------------------------------------------------------|
| 0.1         | Read-only YouTube Music liked-songs scanner + local snapshots         |
| 0.2         | YouTube Data API + classifier + cross-source compare/issues           |
| 0.2.1       | Explored ytmusicapi OAuth (Device Code) to escape browser-header cookie staleness — abandoned: ytmusicapi 1.12 + Google's current backend reject every non-TV `clientName` for OAuth-issued tokens, and the TV clients return YouTube-shape responses ytmusicapi can't parse. Notes archived at [docs/notes/ytmusic-oauth-tvhtml5-fallback.md](docs/notes/ytmusic-oauth-tvhtml5-fallback.md). Salvaged: `fetch_liked_songs` parse-error boundary so stale auth surfaces as a clean re-auth hint instead of a ytmusicapi traceback. |
| **0.2.2**   | Auth/UX polish: `--from-browser` flag on `auth ytmusic` reads YT Music cookies straight from a logged-in browser via [browser-cookie3](https://pypi.org/project/browser-cookie3/) and writes a ytmusicapi-compatible `browser.json` (POSIX mode `0o600`). Manual paste flow stays as a fallback. The TVHTML5 OAuth path documented in [docs/notes/ytmusic-oauth-tvhtml5-fallback.md](docs/notes/ytmusic-oauth-tvhtml5-fallback.md) remains shelved unless cookie extraction fails on a target platform. |
| **0.3**     | Matching engine: ghost YouTube likes (deleted/private/unavailable, detected at `scan youtube-likes` time via `videos.list`) and metadata drift (snapshot-pair title/artists comparison via [RapidFuzz](https://github.com/maxbachmann/RapidFuzz)). Both surface as new `issue_type` rows on `compare-likes`; no new commands. |
| **0.3.1**   | Region-aware ghost detection: `videos.list?part=status,contentDetails` checks `regionRestriction` against the user's configured ISO 3166-1 alpha-2 region (`config.json` or `--region` flag). Region-blocked videos surface as `unavailable_video` findings with `unavailable_reason="region_blocked"`. No new commands, quota cost unchanged. |
| 0.4         | Write actions for cross-source like sync (planned)                    |
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

**YouTube Music** (browser-header flow, auto-extracted from your browser):

```bash
uv run likesurgeon auth ytmusic --from-browser chrome
```

Replace `chrome` above with whichever browser you're signed into music.youtube.com
on. Supported lowercase names (13 total): `chrome`, `chromium`, `firefox`, `edge`,
`brave`, `safari`, `opera`, `opera_gx`, `librewolf`, `vivaldi`, `arc`, `w3m`,
`lynx`. The command reads cookies from that browser's local store and writes
`~/.like-surgeon/browser.json` (POSIX mode `0o600`) — no DevTools copy-paste
required.

> **macOS quirk.** Chrome (and Chromium-family browsers) on macOS encrypt
> their cookie store with a Keychain entry; the first run prompts you to
> allow `python` (or `Terminal`) to access it. Firefox usually avoids that
> prompt because it stores cookies in plain SQLite. Safari may still be
> blocked by macOS privacy settings — if extraction fails, give Terminal
> (or your IDE) **Full Disk Access** in System Settings → Privacy &
> Security and retry.

If `--from-browser` doesn't work in your environment (sandboxed browser,
headless server, locked DB), fall back to the manual flow:

```bash
uv run likesurgeon auth ytmusic
```

This prints the four-step `ytmusicapi browser` paste recipe.

> **Treat `~/.like-surgeon/browser.json` like a session token.** It contains
> live YouTube Music cookies — anyone who reads the file can act as you on
> music.youtube.com until those cookies rotate. The tool stores it with
> POSIX mode `0o600` (owner read/write only). Don't commit it, share it,
> or leave it in shared filesystems.

**YouTube Data API** (Google OAuth, desktop client):

```bash
uv run likesurgeon auth youtube
```

The first run prints a 6-step setup walkthrough that ends with you placing a `youtube-oauth-client.json` (downloaded from Google Cloud Console as a "Desktop app" OAuth client) into `~/.like-surgeon/`. Run the command again and it will open your browser for consent and persist the resulting refresh token to `~/.like-surgeon/youtube-token.json`.

### 3. Scan

**Optional but recommended: configure your region.** Region-blocked videos only get classified as ghosts when likesurgeon knows your region. Create `~/.like-surgeon/config.json`:

```json
{
  "region": "KR"
}
```

Use the [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) code for your country. Without this, `scan youtube-likes` prints a one-time warning and falls back to status-only ghost detection (the 0.3 behavior).

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
uv run likesurgeon issues --type unavailable_video
uv run likesurgeon issues --type metadata_drift
uv run likesurgeon issues --min-confidence 0.7 --format json
```

`compare-likes` requires a snapshot from each source. It prints a bucket summary and persists the run as a `Diagnosis`. `issues` then surfaces the per-item breakdown.

## Caveats

- **`ytmusicapi` is community-maintained.** YouTube Music has no official public API — if a scan fails, check the [`ytmusicapi` issue tracker](https://github.com/sigma67/ytmusicapi/issues).
- **YouTube Data API quota.** A scan of 5000 likes is ~200 quota units (≈100 `playlistItems.list` + ≈100 `videos.list?part=status,contentDetails` for ghost detection — `contentDetails` adds the `regionRestriction` field needed in 0.3.1, but `videos.list` is 1 unit/call regardless of `part=` selection); the default daily quota is 10000. Re-scanning a few times a day is fine.
- **Music classification is heuristic.** Edge cases will misclassify (e.g. covers labelled "tutorial"). The `compare-likes` output is a *starting point* for review, not a verdict — nothing is mutated on the user's behalf.

## Local layout

```
~/.like-surgeon/
├── like-surgeon.sqlite        # all snapshots, tracks, diagnoses
├── browser.json               # ytmusicapi browser-header auth
├── youtube-oauth-client.json  # downloaded Google OAuth client (Desktop app)
├── youtube-token.json         # persisted refresh token (created on first auth)
└── config.json                # optional user settings (e.g. {"region": "KR"})
```

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run pytest               # unit suite; live e2e is deselected by default
uv run pytest -m live       # opt-in: needs a logged-in music.youtube.com session
```

The `live` test extracts cookies from a real browser, writes `browser.json`, and round-trips a `fetch_liked_songs` call against `music.youtube.com`. Set `LIKESURGEON_LIVE_BROWSER=firefox` (or any other supported name) to point it at a browser other than `chrome`.

For deeper context:
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system overview, module responsibilities, DB schema, key design decisions.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — test strategy, auth setup details, debugging tips, release process.

## License

MIT — see [LICENSE](LICENSE).
