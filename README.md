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
