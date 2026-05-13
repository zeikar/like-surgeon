# Architecture

likesurgeon is a local-first, read-only scanner that diagnoses inconsistencies between your YouTube Music likes and your YouTube liked videos. No server, no destructive actions, no third-party data flow.

## System overview

```
┌──────────────┐         ┌──────────────┐
│ YouTube Music│         │ YouTube Data │
│ (ytmusicapi) │         │   API v3     │
└──────┬───────┘         └──────┬───────┘
       │ scan ytmusic           │ scan youtube-likes
       ▼                        ▼
   ┌──────────────────────────────────┐
   │  ~/.like-surgeon/                │
   │   ├─ like-surgeon.sqlite (state) │
   │   ├─ browser.json     (auth)     │
   │   ├─ youtube-token.json (auth)   │
   │   └─ config.json      (settings) │
   └──────────────────────────────────┘
                  │
                  ▼ compare-likes
       ┌────────────────────┐
       │ Diagnosis +        │
       │ DiagnosisItem rows │
       └────────────────────┘
                  │
                  ▼ issues / doctor
            user-facing output
```

Two providers feed into one local SQLite database. Every snapshot is point-in-time and immutable. `compare-likes` is the only computation that crosses sources; everything else is per-source or read-only.

## Data flow

1. **Auth** — `auth ytmusic` writes `browser.json` from a logged-in browser cookie store. `auth youtube` runs the OAuth Desktop-app consent flow and persists `youtube-token.json`.
2. **Scan** — `scan ytmusic` / `scan youtube-likes` fetch the user's likes, translate provider dicts via per-source translators in [src/likesurgeon/snapshot.py](../src/likesurgeon/snapshot.py), upsert `Track` rows (deduplicated by `(source, dedupe_key)`), and write a fresh `Snapshot` plus N `SnapshotItem` rows. Each snapshot is independent — old ones are never mutated.
3. **Compare** — `compare-likes` loads the latest snapshot from each source, runs the three-stage matcher (`video_id` → `canonical_key` → RapidFuzz fuzzy), classifies findings into buckets, and persists the run as a `Diagnosis` plus per-finding `DiagnosisItem` rows.
4. **Inspect** — `issues`, `doctor`, `diff`, and `export` all read from local DB only. No outbound network.

## Modules

| Module | Responsibility | Notes |
|---|---|---|
| [`cli.py`](../src/likesurgeon/cli.py) | Typer entrypoints, command wiring, output formatting | Read-only except for local DB writes |
| [`config.py`](../src/likesurgeon/config.py) | App-dir paths, env overrides, region validation, `config.json` loader | `Config` is a frozen dataclass; `_validate_region` enforces `^[A-Z]{2}$` |
| [`db.py`](../src/likesurgeon/db.py) | SQLAlchemy engine, session factory, schema init | `init_db` creates tables on first run; `_migrate_in_place` adds new nullable columns idempotently on every engine build |
| [`models.py`](../src/likesurgeon/models.py) | ORM tables: `Track`, `Snapshot`, `SnapshotItem`, `Diagnosis`, `DiagnosisItem` | See "Database schema" below |
| [`ytmusic_client.py`](../src/likesurgeon/ytmusic_client.py) | Browser-header auth + ytmusicapi wrapper | Cookie extraction via [browser-cookie3](https://pypi.org/project/browser-cookie3/) |
| [`youtube_client.py`](../src/likesurgeon/youtube_client.py) | OAuth + YouTube Data API v3 wrapper | `_videos_list` fetches `part=status,contentDetails` for ghost detection |
| [`snapshot.py`](../src/likesurgeon/snapshot.py) | Translator dispatch + `Track`/`Snapshot`/`SnapshotItem` writer | Only writer of `Track` rows |
| [`classify.py`](../src/likesurgeon/classify.py) | Music-candidate heuristic for YouTube videos | Pure-functional |
| [`normalize.py`](../src/likesurgeon/normalize.py) | Canonical-key generation (lowercase, strip decorations) | Pure-functional |
| [`compare.py`](../src/likesurgeon/compare.py) | Three-stage cross-source matcher → `CompareResult` | Pure-functional, multiset semantics |
| [`drift.py`](../src/likesurgeon/drift.py) | Snapshot-pair metadata-drift detector | Same-source, two snapshots |
| [`diagnosis.py`](../src/likesurgeon/diagnosis.py) | `CompareResult` → `Diagnosis` + `DiagnosisItem` row writer | |
| [`diff.py`](../src/likesurgeon/diff.py) | Two-snapshot membership diff (added / removed / shared) | Identity = `track_id` |
| [`export.py`](../src/likesurgeon/export.py) | Snapshot → JSON (other formats are future plug-ins) | |
| [`doctor.py`](../src/likesurgeon/doctor.py) | Health summary across snapshots + latest diagnosis | Read-only |
| [`dtos.py`](../src/likesurgeon/dtos.py) | Pydantic DTOs for CLI/JSON serialization | |

## Database schema

Five tables, all SQLite-backed at `~/.like-surgeon/like-surgeon.sqlite`:

- **`tracks`** — deduplicated identity per source. PK `id`. UNIQUE `(source, dedupe_key)` where `dedupe_key` is `video_id` when present, else `canon:<canonical_key>`. Holds the *latest* known metadata for an identity (overwritten on each scan).
- **`snapshots`** — point-in-time scan capture. `(source, created_at, raw_count)`. Immutable.
- **`snapshot_items`** — membership rows linking `snapshot_id` × `track_id` × `position`. **Holds frozen point-in-time metadata**: title, artists, album, duration, etc. as the provider returned them at scan time. This is what makes `export <snapshot_id>` faithful even after a future re-credit.
  - `is_music_candidate` / `music_candidate_score` / `music_candidate_reason` — populated for YouTube rows only (ytmusic is always music).
  - `is_available` / `unavailable_reason` — ghost detection result, populated for YouTube rows only. Reasons: `deleted`, `rejected`, `region_blocked` (0.3.1+).
- **`diagnoses`** — one row per `compare-likes` run. References both source snapshots.
- **`diagnosis_items`** — findings: `issue_type` ∈ {`possibly_missing_from_ytmusic`, `possible_pointer_drift`, `ytmusic_only`, `unavailable_video`, `metadata_drift`, `duplicate_in_source`}, plus `confidence`, `reason`, optional `source_track_id` / `related_track_id`.

**Lightweight in-place migration only.** [`_migrate_in_place`](../src/likesurgeon/db.py) (called from `make_engine` on every CLI run) idempotently issues `ALTER TABLE ... ADD COLUMN` for nullable columns added after a table was first created — that's how 0.3's `is_available` / `unavailable_reason` reach pre-0.3 DBs without forcing a re-scan. There is no Alembic-style framework, so any **breaking** change during 0.x (renamed columns, type changes, FK reshuffles) requires dropping `~/.like-surgeon/like-surgeon.sqlite` and re-scanning. The DB only holds derived data; no original-source state is lost.

## Key design decisions

**Why ghost detection runs at scan time, not compare time.** `videos.list?part=status,contentDetails` is cheap (1 quota unit per 50-video batch) and the ghost reason is point-in-time data that belongs frozen on `SnapshotItem`. Doing it at compare time would either re-issue the API call or read potentially stale `Track` rows; neither matches the snapshot guarantee.

**Why region validation fails fast (and where).** A `config.json` with `{"region": "KOREA"}` would silently misclassify every video with a non-empty `regionRestriction.allowed` list as `region_blocked`. To prevent that, [`_validate_region`](../src/likesurgeon/config.py) enforces `^[A-Z]{2}$` and raises `InvalidRegionError` at three layers:
1. `Config.load()` (config-time) — caught in `_safe_config_load` → `_fail(code=2)`
2. Typer `_parse_region_flag` callback (`--region` CLI arg) — converted to `BadParameter` (exit 2)
3. The CLI prints a one-time warning when no region is configured at all

The regex is intentionally shape-only; "ZZ" or other unassigned-but-shape-valid codes pass through (known limitation, low real-world impact).

**Why `--region` is one-off, never persisted.** A CLI flag is for experiments. Persisting it would make scans non-reproducible across invocations and add an opaque "where did this come from" question. If you want it permanent, write it to `config.json`.

**Why `Track` is mutable but `SnapshotItem` is frozen.** `Track` is identity (does this video exist on this provider?), so it makes sense for the row to track the latest title / album / duration. `SnapshotItem` is history (what did this look like when I scanned?), and the whole point of "backup" is that a re-credit on the provider doesn't rewrite your archive.

**Why `compare.py` is pure-functional.** Cross-source matching is the most complex logic in the codebase. Keeping it I/O-free means it's exhaustively unit-testable with lightweight stand-in items (no DB, no fixtures), which is exactly what `tests/test_compare.py` does. Stage 4 (`stage4_enrich_drift` + `apply_stage4_result`, added in 0.6) preserves this invariant — it takes pre-fetched `CanonicalMetadata` as a plain dict and operates on `Stage4Candidate` primitive dataclasses; the CLI orchestration layer in `cli.py` does the YouTube `videos.list` fetch and degraded-mode handling.

**Stage 4 — drift detection via YouTube enrichment.** When YouTube auth is configured, `compare-likes` runs an extra pass after stages 1-3. It builds a deduped union of unmatched candidates on both sides (ytmusic_only + youtube unmatched, including ghosts) keyed by their original `enumerate(items)` index, calls `videos.list snippet,contentDetails` in 50-vid batches, and promotes pairs matching `(snippet.channelId, contentDetails.duration ±2s, normalize_for_match(snippet.title))` into `pointer_drift_candidates`. Triple-signal match is required for the 0.95 confidence tier; ambiguous candidates within the duration window are rejected. Distinct channel sources to remember: `videos.list snippet.channelId` is the *uploader*; `playlistItems.list snippet.channelId` is the playlist *owner*; the uploader from playlistItems is `snippet.videoOwnerChannelId`. Stage 4 uses `videos.list` exclusively for authoritative metadata.

**Why two YouTube clients.** YouTube Music has no official public API; `ytmusicapi` is community-maintained and uses browser-cookie auth. YouTube Data API v3 is official, OAuth-based, quota-bounded. The two clients have different failure modes (cookie staleness vs. token refresh vs. quota), so they live in separate modules with provider-specific exception types.

## Roadmap context

- **0.1**: read-only YT Music scanner + snapshots
- **0.2**: YouTube Data API + classifier + cross-source `compare-likes` / `issues`
- **0.2.1**: ytmusicapi OAuth exploration — abandoned (see `docs/notes/ytmusic-oauth-tvhtml5-fallback.md`)
- **0.2.2**: `--from-browser` cookie extraction (no more DevTools paste)
- **0.3**: matching engine (three-stage, multiset semantics, ghost detection, drift)
- **0.3.1**: region-aware ghost detection (`regionRestriction` + `config.json`)
- **0.4**: write actions for cross-source like sync (source-of-truth = YouTube)
- **0.5**: `sync` learns `duplicate_in_source` (ytmusic source, N=2) — one `rate_song("INDIFFERENT")` per finding, terminal after attempt
- **0.6**: `compare-likes` Stage 4 — `videos.list` enrichment of unmatched candidates on both sides + triple-match `(channel_id, duration_seconds ±2s, normalize_for_match(title))` promotes `pointer_drift_candidates`. Catches label re-upload drift that stages 1-3 miss. Snapshots stay frozen.
- **0.7**: `possibly_missing_from_ytmusic` sync rewrite — replaces silent-no-op `rate_song("LIKE")` with `videos.rate("none") → videos.rate("like") → 5s → is_in_liked_songs verify`. Three new sub-kinds: `ytm_like_yt_unlike` (rate none), `ytm_like_yt_relike` (rate like), `ytm_like_verify` (read verify). 3-way outcome: `applied` (verify found), `skipped` (verify-miss; terminal), `failed` (HTTP error). `YouTubeClient.rate_video` gains transparent retry (2× on 5xx/429, backoff 0.5s/1.0s).
- **1.0**: local web UI / Electron app
