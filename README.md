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
