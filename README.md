# like-surgeon

> Diagnose and repair mismatches between your YouTube Music liked songs and YouTube likes.

**Status:** Local-first CLI, no server. Per-release scope and changelog: [GitHub Releases](https://github.com/zeikar/like-surgeon/releases).

**The model.** YouTube Liked videos (**LL**) is the only store of likes. YT Music Liked songs (**LM**) is a rendering of it, in the same order, and each LM entry can be shown as a different video than the YouTube video whose like backs it. Below, **A** is the YouTube video whose like backs an LM entry and **B** is the video that entry shows (relinked tracks, official MVs shown as their audio track, and duplicates all follow from this). Details: [docs/design/ll-lm-alignment.md](docs/design/ll-lm-alignment.md).

## What it does today

- Authenticates with YouTube Music (`ytmusicapi`, browser-header) and YouTube Data API v3 (Google OAuth, desktop client).
- Snapshots `ytmusic_liked_songs` (YouTube Music likes) and `youtube_liked_videos` (YouTube LL playlist) into a local SQLite DB.
- Diffs any two snapshots, exports any snapshot to JSON.
- Classifies YouTube liked videos as music-candidate / not via title and channel heuristics.
- `compare-likes` aligns the latest LL and LM scans by order — position reveals which YouTube like backs each LM entry — and persists the findings as a `Diagnosis`. Snapshots stay frozen.
- Detects ghost YouTube likes (deleted, private, region-blocked, unavailable) at scan time, and metadata drift (title or artists changes) between snapshots. Region-blocked detection needs a country code in `config.json` (see Scan).
- `sync` repairs the inconsistencies the alignment finds, on YouTube only, with an LM-wide check after every unlike and an automatic re-like if YT Music doesn't change as expected.
- `issues` lists the latest diagnosis with type/confidence filters and JSON output.
- `doctor` is multi-source: per-source counts, latest diagnosis, LM backed % (share of YT Music liked songs traced to the YouTube like behind them).

Snapshots preserve **point-in-time metadata** — the title, channel, description, etc. that the provider returned at scan time are frozen on each `SnapshotItem`. A later rename in YouTube Music or YouTube doesn't rewrite history.

## Roadmap

Shipped per-release scope and the full changelog live in [GitHub Releases](https://github.com/zeikar/like-surgeon/releases). 0.11 shipped the LL→LM alignment redesign of `compare-likes` / `sync`. Next: **1.0** — local web UI / Electron app.

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

Use the [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) code for your country. Without this, `scan youtube-likes` prints a one-time warning and falls back to status-only ghost detection (no region check).

> `fuzzy_threshold` in `config.json` is deprecated: it is ignored (with a warning from `compare-likes`) now that matching is order-based.

```bash
uv run likesurgeon scan ytmusic                 # YT Music likes
uv run likesurgeon scan youtube-likes           # YouTube LL playlist
```

Scans always fetch the full list — there is no `--limit`, because aligning a truncated list is meaningless.

> **Cookie staleness.** YT Music browser cookies extracted from Chrome went stale within about an hour in practice. If `scan ytmusic` reports a logged-out response, re-run `auth ytmusic --from-browser chrome` and scan again right away.

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
uv run likesurgeon issues --type relinked
uv run likesurgeon issues --type shadow_duplicate
uv run likesurgeon issues --type metadata_drift
uv run likesurgeon issues --min-confidence 1.0 --format json
```

`compare-likes` requires a snapshot from each source. It aligns the two lists (LIS anchors, then in-order pairing of the gaps between them), fetches `videos.list` metadata for each aligned pair, and persists the run as a `Diagnosis`. The summary shows the LM / LL counts, how many LM entries are backed by their own video, and one row per finding type (the three pair types as `write-eligible / total`). It warns when one of our own `sync` attempts happened after the older of the two scans (re-scan both, then re-run). Without YouTube auth the metadata fetch is skipped and every pair is report-only. `issues` surfaces the per-item breakdown.

| `issue_type` | Meaning | `sync` |
|---|---|---|
| `relinked` | A YouTube like (A, unavailable) is shown in YT Music as another video B, B not liked | repoint |
| `rendered_as_other` | Same, but A is not known to be unavailable (usually a playable MV or fan upload; unknown availability is report-only) | repoint, only with `--include-playable` |
| `shadow_duplicate` | A is shown as B, and B is also liked, so YT Music shows B twice | unlike A if A is unavailable; playable A only with `--include-playable` |
| `dead_unrendered` | Unavailable YouTube like not shown in YT Music | report only (a restriction can lift) |
| `unrendered_music` | Playable, music-looking YouTube like not shown in YT Music | report only |
| `unbacked_lm_entry` | YT Music entry with no determinable YouTube like behind it | report only |
| `metadata_drift` | Title/artists changed between two scans of one source | report only |

A pair (A shown as B) is **write-eligible** only at confidence 1.0: A's availability is known and — except for `shadow_duplicate` — it passed the same-recording check (duration within ±3 s, and same channel or overlapping titles). A shadow is usually a different upload (MV, fan or making-of video), so instead `sync` proves it while acting: B must show twice before, and exactly one B must disappear after, or A is re-liked. Everything else is report-only. LL videos in gaps whose LM and LL counts differ get no finding.

`dead_unrendered` is yours to clean up by hand. A region block can lift, so unlike a blocked video only once you have liked a replacement. A **deleted** video can't be unliked through the YouTube Data API (`videos.rate` returns 404), but YT Music's rating call still removes the like — ytmusicapi `rate_song(videoId, "INDIFFERENT")`; check with `videos.getRating`, which still reads deleted videos. The video isn't shown in YT Music, so no other entry depends on it.

### 6. Sync (write actions)

`sync` applies the latest diagnosis's write-eligible findings, on YouTube only (it never calls YT Music `rate_song`). Default is **actually write** after a y/N prompt — use `--dry-run` to see the plan without writing, `--yes` to skip the prompt, `--limit N` to ramp, `--include-playable` to also act on playable originals. Before the prompt it prints the plan, warns if the diagnosis looks stale (a newer scan exists than the one it used, or a scan it used is over an hour old), and names any video an earlier run may have left unliked.

```bash
uv run likesurgeon sync --dry-run                 # plan + quota estimate, no writes
uv run likesurgeon sync                            # apply, with confirmation prompt
uv run likesurgeon sync --yes                      # apply, no prompt
uv run likesurgeon sync --limit 20 --yes           # apply first 20 actions only
uv run likesurgeon sync --include-playable         # also MV / fan-upload originals
uv run likesurgeon skip 812 813                    # keep sync off findings #812, #813 (unskip to undo)
```

#### Actions

| Action | For | Steps | Quota (`videos.rate` 50 units, `getRating` 1) |
|---|---|---|---|
| repoint | `relinked`; `rendered_as_other` with `--include-playable` | confirm A still liked, confirm B isn't liked yet, like B, confirm via `getRating`, unlike A, confirm, full-LM check | 104 units |
| unlike_shadow | `shadow_duplicate` (A unavailable, or playable with `--include-playable`) | confirm B liked and shown ≥2×, confirm A still liked, unlike A, confirm, full-LM check | 53 units |

**LM check and restore.** After each unlike `sync` reads the whole LM again: repoint expects it unchanged, unlike_shadow expects one B fewer (B still shown). On a mismatch it re-reads once after 15 s; if it still differs, A is re-liked and confirmed. Unliking a wrongly paired A would otherwise silently drop an unrelated song. A restore re-like (~51 units) happens only when the unlike didn't take or the check failed. For a repoint, the undo of the B like it added (~51 units) happens whenever A stays liked after B's like may have landed: after a restore, when the quota rejects A's unlike, or when B's like errors but landed. Neither is in the plan's estimate. Any action that needs a restore, successful or not, and any B undo that can't be confirmed also stops the run, since other pairs from the same diagnosis may be wrong too. The default daily YouTube quota is 10,000 units; the plan prints the estimate (warning above 10,000), and `--limit` slices a run.

`--include-playable` is off by default: it removes a real like you made (an official MV, a fan upload) so that YT Music shows only the audio track. Keep individual findings out with `skip`.

#### If something goes wrong

- **Restored** — A was re-liked and the finding marked `skipped`, because A's unlike didn't take or the LM check didn't match. The run stops; re-scan both sources and run `compare-likes` before syncing again. Separately, for a repoint `sync` tries to unlike the B it liked, whenever A stays liked (also when A's unlike was rejected by the quota, or B's like errored but landed). If that undo fails or can't be confirmed, `sync` names the video and stops the run: B may still be liked, so unlike it on YouTube by hand if you don't want it (no song is lost).
- **Stale finding** — A is no longer liked on YouTube (you unliked it after the scan), or B is already liked, so the finding is `skipped` without any write.
- **Stranded** — A was unliked and re-liking it failed or couldn't be confirmed, or the run was interrupted after the unlike: the song may now be liked nowhere. `sync` lists stranded videos on every run until a later YouTube scan contains them again. Check YT Music Liked songs; if the song is missing, re-like the original on YouTube, then re-scan.
- **Aborted or quota stop** — remaining actions stay `open`. Re-scan both sources, run `compare-likes`, then `sync` again.

#### State model

- `skipped` is terminal until you run `likesurgeon unskip <id>`: a finding becomes `skipped` when B's like didn't land, a precheck failed, A was restored, or you ran `skip`.
- `skipped` carries over to later diagnoses: each `compare-likes` copies it onto the matching new finding.

#### Safety

Sync refuses (exit 1) when:
- any of its own writes happened after the older of the two scans the diagnosis is built from — a write between scans misaligns them, and one after both makes the diagnosis stale. Re-scan both sources, then `compare-likes`.
- YouTube write scope is missing — run `likesurgeon auth youtube` again and accept "Manage your YouTube account".
- the YT Music auth probe fails (needs `browser.json`; re-run `auth ytmusic --from-browser`).

Sync stops early when:
- YT Music liked songs can't be read (no baseline to check against),
- an action had to be restored, or a repoint's B like couldn't be confirmed undone (the diagnosis' pairs can't be trusted), or
- YouTube's daily quota runs out.

The run exits non-zero if any action failed or it stopped early on an unreadable LM, after a restore, or after an unconfirmed B undo.

## Caveats

- **`ytmusicapi` is community-maintained.** YouTube Music has no official public API — if a scan fails, check the [`ytmusicapi` issue tracker](https://github.com/sigma67/ytmusicapi/issues).
- **Order alignment has limits.** YT Music exposes no backing id, so pairing relies on order. Gaps where the LM and LL counts differ stay report-only, and the YouTube API may stop at 5000 likes (unverified) — if your library is that large, compare the counts in `doctor` with what you see on YouTube.
- **YouTube Data API quota.** A scan of 5000 likes is ~200 quota units (≈100 `playlistItems.list` + ≈100 `videos.list?part=status,contentDetails` for ghost detection — `contentDetails` adds the `regionRestriction` field for region-blocked detection, but `videos.list` is 1 unit/call regardless of `part=` selection); the default daily quota is 10000. Re-scanning a few times a day is fine.
- **Music classification is heuristic.** Edge cases will misclassify (e.g. covers labelled "tutorial"). The `compare-likes` output is a *starting point* for review, not a verdict — only `sync` writes, and only write-eligible findings.

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
