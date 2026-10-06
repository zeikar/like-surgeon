<p align="center">
  <img src="https://raw.githubusercontent.com/zeikar/like-surgeon/main/docs/assets/hero.jpg" alt="like-surgeon: back up your YouTube Music liked songs, find relinked, duplicate and dead likes, and fix the ones it can prove" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/likesurgeon/"><img src="https://img.shields.io/pypi/v/likesurgeon" alt="PyPI version"></a>
  <a href="https://pypi.org/project/likesurgeon/"><img src="https://img.shields.io/pypi/pyversions/likesurgeon" alt="Python versions"></a>
  <a href="https://github.com/zeikar/like-surgeon/actions/workflows/ci.yml"><img src="https://github.com/zeikar/like-surgeon/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/zeikar/like-surgeon/blob/main/LICENSE"><img src="https://img.shields.io/github/license/zeikar/like-surgeon" alt="MIT license"></a>
</p>

# like-surgeon

**Back up your YouTube Music liked songs, find relinked, duplicate and dead likes, and fix the ones it can prove.**

like-surgeon is a local-first command-line tool. It snapshots your YouTube Music *Liked songs* and your YouTube *Liked videos* into a SQLite file on your machine, works out which YouTube like is behind every song, and repairs the cases it can prove — with a check after every change. No server, no account of ours: your data and credentials stay on your computer.

## Why

Use YouTube Music for a few years and your Liked songs drifts:

- **Songs YT Music swapped behind your back.** The upload you liked was pulled or blocked in your region, so YT Music now plays a different release of the song — but your YouTube likes still hold the old, dead video.
- **The same song twice.** You liked the official music video *and* the audio track; YT Music shows both as the same track.
- **Songs that are just gone.** A deleted or region-blocked video stays in your likes, invisible in YT Music.
- **No backup.** YouTube Music has no in-app export for your likes, and titles change over time.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/zeikar/like-surgeon/main/docs/assets/how-it-works.png" alt="Diagram: YouTube Liked videos on the left, YT Music Liked songs on the right, lined up by position. A dead upload shown as a new release is re-pointed, a music video shown as a duplicate audio track can be unliked (opt-in while it still plays), and a deleted video is reported." width="100%">
</p>

Your YouTube **Liked videos** list is the only place likes are stored. YT Music **Liked songs** is a view of it — same order, filtered to what YT Music shows, and each entry may play a different video than the like behind it. YT Music never says which like backs which song, so like-surgeon lines the two lists up by position (no fuzzy title matching) and repairs the mismatches it can prove, on YouTube. The full model and the evidence for it: [docs/design/ll-lm-alignment.md](https://github.com/zeikar/like-surgeon/blob/main/docs/design/ll-lm-alignment.md). (The docs call the two lists **LL** and **LM**, after their playlist ids.)

## Quick start

Requires Python ≥ 3.11.

```bash
uv tool install likesurgeon          # or: pipx install likesurgeon
likesurgeon init
likesurgeon auth ytmusic --from-browser chrome   # reads cookies from a browser signed in to music.youtube.com
likesurgeon auth youtube                          # prints the Google Cloud setup steps; run it again to sign in
likesurgeon scan ytmusic
likesurgeon scan youtube-likes --region US        # your country code, so region-blocked videos are caught
likesurgeon compare-likes                         # what's wrong, and what can be fixed
likesurgeon sync --dry-run                        # the plan, without writing anything
```

<p align="center">
  <img src="https://raw.githubusercontent.com/zeikar/like-surgeon/main/docs/assets/terminal.png" alt="Terminal: compare-likes finds 13 relinked songs, 3 shadow duplicates and 6 dead likes in a library of about 1,200; sync --dry-run plans 11 repoints and 2 shadow unlikes for 1,250 quota units" width="640">
</p>

Just want a backup? `auth ytmusic`, `scan ytmusic` and `export` are enough — no Google Cloud setup needed.

## What it finds

`compare-likes` lines up the latest scans of both lists and records one finding per mismatch. **A** is the YouTube video whose like is behind a YT Music entry, **B** is the video that entry plays.

| Finding | What happened | What `sync` does (proven pairs only) |
|---|---|---|
| `relinked` | A is unavailable, and YT Music plays B instead; B isn't liked | **repoint**: like B, then unlike A |
| `rendered_as_other` | A isn't known to be unavailable (usually a playable MV or fan upload) but YT Music shows B | repoint with `--include-playable`, if A is known to play |
| `shadow_duplicate` | A is shown as B while B is also liked, so B appears twice | **unlike A**; a playable A only with `--include-playable` |
| `dead_unrendered` | A is unavailable and not shown in YT Music at all | report only — [clean up by hand](https://github.com/zeikar/like-surgeon#dead-likes) |
| `unrendered_music` | A plays fine and looks like music, but isn't shown in YT Music | report only |
| `unbacked_lm_entry` | a YT Music entry with no like that can be pinned behind it | report only |
| `metadata_drift` | a title or artist changed between two scans | report only |

## Safety

- **Read-only until you say so.** Only `sync` writes, after showing the plan and a y/N prompt; `--dry-run` writes nothing, `--limit N` ramps up, `skip <id>` keeps a finding out for good.
- **Only proven pairs.** A pair is acted on only when its gap in the alignment is one-to-one and A's availability is known; repoints also need the same recording (duration within ±3 s and same channel or overlapping titles).
- **Every unlike is checked.** After each change `sync` confirms the rating and re-reads your whole Liked songs list. If anything else moved, A is re-liked and the run stops.
- **Writes only on YouTube.** YT Music's own like button is never pressed: it shares the same rating, and pressing it duplicates or reverts entries.
- **Local credentials.** Cookies and OAuth tokens stay in `~/.like-surgeon/`; the files the tool writes are owner-only (`0600`) on macOS and Linux.

## Usage

The commands below assume `likesurgeon` is on your `PATH`. From a source checkout, prefix them with `uv run`.

### 1. Initialize local storage

```bash
likesurgeon init
```

Creates `~/.like-surgeon/` and the SQLite database at `~/.like-surgeon/like-surgeon.sqlite`. Override with `LIKE_SURGEON_HOME=/some/path`.

### 2. Authenticate

**YouTube Music** (browser-header flow, auto-extracted from your browser):

```bash
likesurgeon auth ytmusic --from-browser chrome
```

Replace `chrome` with whichever browser you're signed into music.youtube.com on. Supported names: `chrome`, `chromium`, `firefox`, `edge`, `brave`, `safari`, `opera`, `opera_gx`, `librewolf`, `vivaldi`, `arc`, `w3m`, `lynx`. The command reads cookies from that browser's local store and writes `~/.like-surgeon/browser.json` (mode `0600`) — no DevTools copy-paste required.

> **macOS.** Chrome (and Chromium-family browsers) encrypt their cookie store with a Keychain entry; the first run asks you to allow `python` (or Terminal) to access it. Firefox usually avoids the prompt because it stores cookies in plain SQLite. If extraction is still blocked, give Terminal (or your IDE) **Full Disk Access** in System Settings → Privacy & Security and retry.

If `--from-browser` doesn't work in your environment (sandboxed browser, headless server, locked DB), `likesurgeon auth ytmusic` prints the manual recipe instead: copy request headers from DevTools and paste them into `uvx --from ytmusicapi ytmusicapi browser`.

> **Treat `~/.like-surgeon/browser.json` like a session token.** It holds live YouTube Music cookies: anyone who reads it can act as you on music.youtube.com until they rotate. Don't commit it, share it, or leave it on a shared filesystem.

**YouTube Data API** (Google OAuth, desktop client):

```bash
likesurgeon auth youtube
```

The first run prints a 6-step walkthrough: create a free Google Cloud project, enable YouTube Data API v3, and download a "Desktop app" OAuth client as `~/.like-surgeon/youtube-oauth-client.json`. Run the command again and it opens your browser for consent and stores the refresh token in `~/.like-surgeon/youtube-token.json`.

### 3. Scan

**Recommended: set your region.** Region-blocked videos are only detected when like-surgeon knows your country. Create `~/.like-surgeon/config.json`:

```json
{
  "region": "US"
}
```

Use the [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) code for your country, or pass `--region <code>` to `scan youtube-likes`. Without either, each scan warns and checks only for deleted, private and rejected videos — a region-blocked video then counts as playable, so its relink shows up as `rendered_as_other` instead of `relinked`.

```bash
likesurgeon scan ytmusic          # YT Music Liked songs
likesurgeon scan youtube-likes    # YouTube Liked videos
```

Scans always fetch the full list; aligning a truncated list would be meaningless. Each scan is stored as a snapshot with the metadata of that moment, so a later rename doesn't rewrite history.

> **Cookie staleness.** YT Music cookies extracted from Chrome can go stale within about an hour. If `scan ytmusic` reports a logged-out response, re-run `auth ytmusic --from-browser chrome` and scan again right away.

### 4. Inspect and back up

```bash
likesurgeon snapshots
likesurgeon diff <old_snapshot_id> <new_snapshot_id>
likesurgeon export <snapshot_id> --format json --output likes.json
likesurgeon doctor
```

`doctor` summarizes both sources: counts, the latest diagnosis, and how many YT Music songs are traced to the like behind them.

### 5. Compare

```bash
likesurgeon compare-likes
likesurgeon issues
likesurgeon issues --type relinked
likesurgeon issues --min-confidence 1.0 --format json
```

`compare-likes` needs a snapshot of each source. It aligns the two lists (anchors on songs liked as themselves, then pairs the gaps between them in order), fetches `videos.list` metadata for each pair, and saves the result as a diagnosis — see [What it finds](https://github.com/zeikar/like-surgeon#what-it-finds). It warns when one of our own `sync` writes happened after the older of the two scans (re-scan both, then re-run). Without YouTube auth, `relinked` and `rendered_as_other` pairs are report-only. `issues` lists the findings one by one.

A pair (A shown as B) is **write-eligible** only at confidence 1.0: A's availability is known and — except for `shadow_duplicate` — it passed the same-recording check (duration within ±3 s, and same channel or overlapping titles). A shadow is usually a different upload (an MV, a fan or making-of video), so `sync` proves it while acting instead: B must show at least twice before, and exactly one B must disappear after, or A is re-liked. YouTube likes in gaps whose two counts differ get no finding; the YT Music entries there are reported as `unbacked_lm_entry`.

#### Dead likes

`dead_unrendered` findings are yours to clean up by hand. A region block can lift, so unlike a blocked video only once you have liked a replacement. A **deleted** video can't be unliked through the YouTube Data API (`videos.rate` returns 404), but YT Music's rating call still removes the like — ytmusicapi `rate_song(videoId, "INDIFFERENT")`; check with `videos.getRating`, which still reads deleted videos. The video isn't shown in YT Music, so no other entry depends on it.

### 6. Sync (write actions)

`sync` applies the latest diagnosis's write-eligible findings, on YouTube only. It prints the plan and a quota estimate, warns if the diagnosis looks stale (a newer scan exists, or a scan it used is over an hour old), names any video an earlier run may have left unliked, then asks before writing.

```bash
likesurgeon sync --dry-run              # plan + quota estimate, no writes
likesurgeon sync                        # apply, with confirmation prompt
likesurgeon sync --yes                  # apply, no prompt
likesurgeon sync --limit 20 --yes       # apply the first 20 actions only
likesurgeon sync --include-playable     # also act on MV / fan-upload originals
likesurgeon skip 812 813                # keep sync off findings #812 and #813 (unskip to undo)
```

| Action | For | Steps | Quota (`videos.rate` 50 units, `getRating` 1) |
|---|---|---|---|
| repoint | `relinked`; `rendered_as_other` with `--include-playable` | confirm A still liked and B not yet liked, like B, confirm, unlike A, confirm, full Liked songs check | 104 units |
| unlike_shadow | `shadow_duplicate` (A unavailable, or playable with `--include-playable`) | confirm B liked and shown ≥2×, confirm A still liked, unlike A, confirm, full Liked songs check | 53 units |

**Check and restore.** After each unlike `sync` reads the whole Liked songs list again: a repoint expects it unchanged, an unlike_shadow expects one B fewer (B still shown). On a mismatch it re-reads once after 15 s; if it still differs, A is re-liked and confirmed, because unliking a wrongly paired A would otherwise silently drop an unrelated song. For a repoint, the B like it added is undone whenever A stays liked after B's like may have landed (after a restore, when the quota rejects A's unlike, or when B's like errors but landed). A restore (~51 units) and a B undo (~51 units) aren't in the plan's estimate. The default daily YouTube quota is 10,000 units; the plan warns above that, and `--limit` slices a run.

`--include-playable` is off by default: it removes a real like you made (an official MV, a fan upload) so that YT Music shows only the audio track. Keep individual findings out with `skip`.

#### If something goes wrong

- **Restored** — A was re-liked and the finding marked `skipped`, because A's unlike didn't take or the Liked songs check didn't match. The run stops; re-scan both sources and run `compare-likes` before syncing again. If undoing a repoint's B like fails or can't be confirmed, `sync` names the video and stops: B may still be liked, so unlike it on YouTube by hand if you don't want it (no song is lost).
- **Stale finding** — A is no longer liked (you unliked it after the scan), or B is already liked, so the finding is `skipped` without any write.
- **Stranded** — A was unliked and re-liking it failed or couldn't be confirmed, or the run was interrupted after the unlike: the song may now be liked nowhere. `sync` lists stranded videos on every run until a later YouTube scan contains them again. Check YT Music Liked songs; if the song is missing, re-like the original on YouTube, then re-scan.
- **Aborted or quota stop** — remaining actions stay `open`. Re-scan both sources, run `compare-likes`, then `sync` again.

#### State model

- `skipped` is terminal until you run `likesurgeon unskip <id>`: a finding becomes `skipped` when B's like didn't land, a precheck failed, A was restored, or you ran `skip`.
- `skipped` carries over to later diagnoses: each `compare-likes` copies it onto the matching new finding.

#### When sync refuses or stops

Sync refuses (exit 1) when:
- any of its own writes happened after the older of the two scans the diagnosis is built from — a write between scans misaligns them, and one after both makes the diagnosis stale. Re-scan both sources, then `compare-likes`.
- YouTube write scope is missing — run `likesurgeon auth youtube` again and accept "Manage your YouTube account".
- the YT Music auth probe fails (needs `browser.json`; re-run `auth ytmusic --from-browser`).

Sync stops early when:
- YT Music Liked songs can't be read (no baseline to check against),
- an action had to be restored, or a repoint's B like couldn't be confirmed undone (the diagnosis' pairs can't be trusted), or
- YouTube's daily quota runs out.

The run exits non-zero if any action failed or it stopped early on an unreadable Liked songs list, after a restore, or after an unconfirmed B undo.

## Caveats

- **`ytmusicapi` is community-maintained.** YouTube Music has no official public API — if a scan fails, check the [`ytmusicapi` issue tracker](https://github.com/sigma67/ytmusicapi/issues).
- **Order alignment has limits.** YT Music exposes no backing id, so pairing relies on order. Gaps where the two counts differ stay report-only, and the YouTube API may stop at 5000 likes (unverified) — if your library is that large, compare the counts in `doctor` with what you see on YouTube.
- **YouTube's count can be a few higher than a scan's.** An old like can stay in Liked videos (the app and the playlist's `itemCount` count it) while the API reports the video as not liked and leaves it out of `playlistItems`, so scans never see it. It has been seen mostly with videos marked made for kids; they still show in the web list, with no "show unavailable videos" option. Un-liking and re-liking such a video on YouTube fixes it: the API lists it again, and YT Music may pick it up too. `scan youtube-likes` warns with the difference; [how to find them](https://github.com/zeikar/like-surgeon/blob/main/docs/notes/hidden-likes.md).
- **Google sign-in may expire weekly.** While your OAuth consent screen is in *Testing* (the setup `auth youtube` prints), Google expires its refresh tokens after 7 days; if YouTube calls start failing with an auth error, run `likesurgeon auth youtube` again.
- **YouTube Data API quota.** A scan of 5000 likes costs about 200 quota units (≈100 `playlistItems.list` + ≈100 `videos.list` calls for availability and region checks); the default daily quota is 10,000. Re-scanning a few times a day is fine.
- **Music classification is heuristic.** `unrendered_music` relies on title and channel heuristics, so edge cases will misclassify (e.g. covers labelled "tutorial"). Findings are a starting point for review — only `sync` writes, and only write-eligible findings.

## Local layout

```
~/.like-surgeon/
├── like-surgeon.sqlite        # all snapshots, tracks, diagnoses
├── browser.json               # ytmusicapi browser-header auth
├── youtube-oauth-client.json  # your Google OAuth client (Desktop app)
├── youtube-token.json         # refresh token (created on first auth)
└── config.json                # optional settings, e.g. {"region": "US"}
```

## Roadmap

Releases and the full changelog: [GitHub Releases](https://github.com/zeikar/like-surgeon/releases). Next: **1.0** — a local web UI.

## Development

```bash
git clone https://github.com/zeikar/like-surgeon.git
cd like-surgeon
uv sync --extra dev
uv run ruff check .
uv run ruff format .
uv run pytest               # unit suite; live e2e is deselected by default
uv run pytest -m live       # opt-in: needs a logged-in music.youtube.com session
```

The `live` test extracts cookies from a real browser, writes `browser.json`, and round-trips a `fetch_liked_songs` call against `music.youtube.com`. Set `LIKESURGEON_LIVE_BROWSER=firefox` (or any other supported name) to use a browser other than `chrome`.

For deeper context:
- [docs/ARCHITECTURE.md](https://github.com/zeikar/like-surgeon/blob/main/docs/ARCHITECTURE.md) — system overview, module responsibilities, DB schema, key design decisions.
- [docs/DEVELOPMENT.md](https://github.com/zeikar/like-surgeon/blob/main/docs/DEVELOPMENT.md) — test strategy, auth setup details, debugging tips, release process.

## License

MIT — see [LICENSE](https://github.com/zeikar/like-surgeon/blob/main/LICENSE).
