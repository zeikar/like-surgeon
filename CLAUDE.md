# CLAUDE.md

Project-specific instructions for like-surgeon. The global ~/.claude/CLAUDE.md still applies; this only adds project conventions.

## Documentation map

- [README.md](README.md) — user-facing install/usage, findings table, sync actions, caveats
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map, data flow, DB schema, design decisions
- [docs/design/ll-lm-alignment.md](docs/design/ll-lm-alignment.md) — the LL→LM alignment spec (domain model, findings, actions)
- [docs/design/canonicalize.md](docs/design/canonicalize.md) — the `canonicalize` spec (move a like to the version the artist page lists)
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — test strategy, fake/monkeypatch patterns, auth setup, debugging
- [docs/design/playlist-repoint.md](docs/design/playlist-repoint.md) — the `repoint-playlists` spec (model, finding, action flow, outcomes, limits)
- [docs/design/playlist-canonicalize.md](docs/design/playlist-canonicalize.md) — the `canonicalize-playlists` spec (move a playlist item to the release the artist page lists: repoint or merge)
- CLAUDE.md (this file) — conventions and gotchas that don't fit the above

## Domain model: LL is the store, LM renders it (read this first)

Full spec: [docs/design/ll-lm-alignment.md](docs/design/ll-lm-alignment.md). Internalize this before touching `align.py`, `diagnosis.py`, or `sync*.py`.

- **YouTube Liked videos (LL) is the only store of likes.** YouTube Music Liked songs (LM) is a *rendering* of LL: same order, filtered to what YT Music shows, each entry displayed as a playable track whose `videoId` (B) can differ from the YouTube video (A) whose like backs it.
- A relink is not a like that moved from A to B — it is A's like *shown as* B. Official MVs / fan uploads shown as their audio track ("shadows"), re-uploads, and duplicates follow from the same thing. If B is also liked on YouTube, LM shows B twice (`shadow_duplicate`).
- LM exposes no backing id, so **order is the only signal**: `align.py` anchors LM entries whose `videoId` is itself in LL (LIS), then pairs the gaps in order. Only gaps with equal counts (*k* = *m*) are paired. Pairs are write-eligible (confidence 1.0) when A's availability is known and — except `shadow_duplicate` — they pass the same-recording check (`videos.list` duration within ±3 s and same channel or overlapping titles). Shadows skip that check because they are different uploads by nature; `unlike_shadow` proves the pair at run time (B shown ≥2× before, exactly one B fewer after, else restore). Everything else is report-only.
- `sync` has three write actions, all on YouTube only (`canonicalize` has its own two, below): **repoint** (like B, confirm, unlike A), **unlike_shadow** (B liked and shown ≥2×: unlike A) and, opt-in, **`relike`** (unlike and re-like an `unrendered_music` A so YT Music may show it). Each is followed by one LM-wide check: after a repoint or unlike_shadow mismatch re-likes A (restore); a `relike` mismatch never restores and only stops the run. Unliking a wrong A would remove an unrelated LM entry, which is why the check exists.

**Rules.** Never call YT Music `rate_song` (same rating as YouTube's; it only duplicates or reverts) — sole exception: unliking a *deleted* video by hand, which `videos.rate` 404s on (README, Compare across sources). Never match by title/fuzzy — sole exception: `canonicalize` and `canonicalize-playlists` picking their target (below). Never re-add `yt_like` / `ytm_dedupe` — removed; see the spec. Safety invariants: see 'Never / always' under Sync state model — read them before touching `sync*.py`.

## Canonicalize (separate command)

Spec: [docs/design/canonicalize.md](docs/design/canonicalize.md). Gotchas:

- Reports are `Diagnosis.kind = 'canonicalize'`; `latest_diagnosis()` defaults to `kind IS NULL`, so `sync` and `doctor` never see them.
- The sole title exception: B is the one ATV with an exact `title_key` match (±1 s) in A's artists' **Show-all** lists. Top songs only count A as listed, never supply a B. That one B credited to a different set of artist **ids** than A (`performers_cause`; ids, never names) makes the finding report-only; two or more B's stay `canonical_ambiguous` whoever performs them. A's like is A's own (never inferred); alignment's same-recording title check only verifies pairs that order already fixed.
- `--apply`: the planned `canonical_repoint` / `canonical_merge` is a forecast; B's live rating picks the flow. A restore stops the run (`CANONICALIZE_STOP`). Never `rate_song` here either.
- `--apply --include-flagged` (opt-in) also takes a 0.5 `canonical_version` whose A appears once in the report's LM snapshot and has a version keyword, and whose A (snapshot row) and B (its `Track.raw_json`) pass `performers_cause`; `flagged_keywords` derives this from that data — never parse `reason`. Different performers is never lifted.
- `canonicalize-playlists` ([docs/design/playlist-canonicalize.md](docs/design/playlist-canonicalize.md)) runs `find_candidates` unchanged once per own playlist with a YouTube side (keep it that way), artist pages read once over the ATV tracks' artist ids. Reports are `Diagnosis.kind = 'playlist_canonicalize'` (`playlist_canonical_version`, on a `playlist_scan_id`); `related_item_id` is A's own YT Music row, B is only on `related_track_id`.
- A must be shown as itself for a playable YouTube item of its own (same video id, `is_available is True`); a B already in the playlist must be listed and shown exactly once and playable (YT Music `isAvailable` not False), else report-only; `--apply` re-checks B's YT Music availability live and never deletes A for an unavailable B. `--include-flagged` lifts only the keyword cause; `plan` recomputes the rest (`performers_cause` from A's YT Music row and B's `Track`, `structural_cause` from the scan) — never parse `reason`.
- Its `--apply` picks the flow from the live baseline: B on the YouTube side → merge (delete A only), else the playlist repoint flow. A merge never touches B (it is the entry being kept): an unconfirmed delete undoes nothing; a mismatch after the delete puts A back and stops; no message may advise removing B. A merge whose A was put back has no settling row until a re-scan.

## Playlist repoint (separate command)

Spec: [docs/design/playlist-repoint.md](docs/design/playlist-repoint.md). Gotchas:

- Reports are `Diagnosis.kind = 'playlists'` (`playlist_relinked`, built on a `playlist_scan_id`); `latest_diagnosis()` defaults to `kind IS NULL`, so `sync` and `doctor` never see them. `--apply` exits 1 when a newer playlist scan exists than the report's.
- Never delete A's item before both sides confirm B at its position (anywhere in an automatically sorted playlist). Never remove B unless A's item is known to be in place, so not after an unconfirmed delete (it may still land; report `delete_unconfirmed` and stop) and not when A can't be put back (`left_removed`). An insert that isn't listed, 2xx or errored, may still land and is not skipped: B goes to `left_inserted`, the item stays open and the run stops.
- Playlist skips carry over per playlist (`carry_over_skipped` also keys on A's playlist id).
- Only an applied `playlist_check` or `playlist_rollback_verify` settles an action. `unsettled_playlist_actions` lists the rest, and its warning is retired only by newer snapshots of that playlist on both sides.
- `playlist_*` rows stay out of `_WRITE_KINDS` and `_SETTLING_KINDS`: they never block `sync` or feed the stranded-likes list. Never rate or touch likes here.
- A playlist whose live state differs from the scan gets no writes (items stay `open`, other playlists continue); an unreadable side, an undo or quota exhaustion stops the run. A wrong B that YT Music renders as itself passes every check, so the same-recording check and the report review are the guard.
- Automatically sorted playlists: a positional insert gets HTTP 400 `manualSortRequired` (`PlaylistManualSortRequired`). The dispatcher records that row as `MANUAL_SORT_REJECTED` (not sent, like `QUOTA_REJECTED`, so `unsettled_playlist_actions` ignores it), switches the playlist to `sorted` for the run and inserts without a position. Every check except B's removal goes through `_only_change` (one moving element, everything else unchanged), with an index in a positional playlist and anywhere in a sorted one. Don't add per-mode comparisons. Applied actions shift later actions' indices (`moves`, identity for a positional repoint, nothing lands in a merge); the precheck still requires A and B (for a canonical action: A shown as itself at *j*) at the shifted indices.
- Quota: about 100 units per item (insert and delete, 50 each) plus the playlist reads; a `canonicalize-playlists` merge is one delete (50) plus one read, and its report costs no YouTube quota (stored scan, YT Music artist pages). Undo calls, re-reads and the one refused insert per sorted playlist aren't estimated. Exits 1 on quota exhaustion.
- Both playlist `--apply` modes go through `cli._playlist_apply`; per-command strings live in `_PlaylistCommand`. B's plan-table title comes from the related `SnapshotItem` for `playlist_relinked` and from the related `Track` for `playlist_canonical_version`; shared result lines must hold for a merge too.

## Releases

Cut a git tag **and** a GitHub release at every milestone bump (0.4.0, 0.4.1, 0.5.0, ...). Without these two artifacts there's no quick changelog and no rollback target — they're cheap, do them every time.

Procedure on `main`, once the feature is merged:

1. Bump `version` in `pyproject.toml` **and** `__version__` in `src/likesurgeon/__init__.py` — these two must stay in lockstep (v0.8.0 shipped with `__init__.py` stale at `0.7.1`, fixed in 0.9.0).
2. Commit `chore: bump version to <X.Y.Z>`.
3. Tag the commit `v<X.Y.Z>` (with the `v` prefix).
4. `git push && git push --tags`.
5. `gh release create v<X.Y.Z> --title "v<X.Y.Z> — <slug>" --notes "..."` — notes summarize what shipped (cribbing from the commit log is fine), include the action-mapping or quota table when behavior changed. Publishing the release uploads to PyPI (`.github/workflows/release.yml`, trusted publishing); the workflow fails if the tag doesn't match both version strings. A PyPI version can't be re-uploaded, so a broken release means a new patch version.

Don't retro-edit a published tag/release for follow-up fixes — cut a fresh patch tag instead.

## CI gate (local checks before push)

```bash
uv run pytest tests/
uv run ruff format --check .
uv run ruff check
```

CI runs all three. `ruff format --check` is the easy miss — `ruff check` alone catches lint but not formatting, and CI WILL fail on unformatted code.

## Sync state model (when touching sync*.py / models)

**Never / always**
- Never overwrite `DiagnosisItem.reason` (diagnosis-time evidence); sync detail goes on `SyncAttempt.reason`.
- Always commit each write's `SyncAttempt` row immediately.
- Never unlike A without `getRating` confirmation, a full-LM check, and a restore (re-like A) on mismatch.
- Never act on unknown availability (`is_available` None).
- `sync` must refuse (exit 1) if any non-skipped sync attempt happened after the older of the diagnosis's two scans; `compare-likes` warns on the same condition. `canonicalize --apply` writes (`canonical_like` / `_unlike` / `_rollback` in `_WRITE_KINDS`) count too.

**Operational notes**
- Planner (`sync.plan`, pure): write-eligible pair findings become `repoint` / `unlike_shadow`; others become planner skips (audit row only, item stays `'open'`). `--include-playable` (off by default) also acts on a playable A (MV / fan upload), which removes a like the user made. `--relike-unrendered` (off by default) turns `unrendered_music` into `relike`, oldest first (highest LL index first), because a re-like moves the video to the top of both lists; without it the plan shows `relike: 0`.
- Dispatcher (`sync_dispatch.execute`): LM check is repoint = unchanged, unlike_shadow = one B fewer (B still ≥1), one 15 s re-read on mismatch. A still liked after its unlike is re-liked first. A failed restore means A is stranded. A `relike`'s LM check always runs once the unlike was sent and accepts nothing removed and at most one entry added (one 15 s re-read when nothing was added). Full flow: spec §4.1. It never counts as restored. Outcomes:
  - +1 entry → applied; no change with every call clean → skipped (terminal).
  - Anything removed, 2+ added, or an unconfirmed unlike or re-like → stop with A liked, or reported stranded if its like can't be confirmed. An unconfirmed re-like gets one more attempt first.
  - An unlike that errored but landed, a quota-rejected unlike, or an unreadable rating at the precheck → `failed` without stopping. An unreadable LM → stop.
  - Known limit: an unlike that lands even after the confirmed re-like isn't detected; an unconfirmed unlike always stops the run so a re-scan follows.
- **Stranded** = A unliked and its re-like failed/unconfirmed, or the action was interrupted after the unlike (`interrupted` row, also on Ctrl-C). `stranded_unliked_video_ids` (newest settling row per video id, across alignment and canonical findings; `canonical_unlike` settles) lists them on every `sync` and `canonicalize --apply` run until a later YouTube scan contains the video. For a `relike` only a `relike_verify` applied newer than the unlike settles it (`relike_like` and `relike_lm_check` never do).
- `DiagnosisItem.status`: `'applied'` only when every call succeeded and the LM check passed (a `relike`'s retried, then confirmed re-like still counts). `'skipped'` is terminal (B's like didn't land, precheck failed, A restored, a video re-liked by `relike` still isn't shown, or `skip`); `unskip` re-opens; it carries over by `(issue_type, source_track, related_track)` (`carry_over_skipped`). Anything else stays `'open'`.
- Every action prechecks A is still liked (stale → `skipped`, no writes); a repoint also requires B not yet liked (else stale → `skipped`) and, whenever A stays liked after B's like may have landed (restore, quota-rejected unlike, B's like erroring), unlikes B (`repoint_rollback*`; unconfirmed → `ExecResult.left_liked`). Any action needing a restore, successful or not, stops the run (`aborted`, re-scan + `compare-likes` needed). LM unreadable before an action stops the run (`aborted`); quota exhaustion stops it after the in-flight action finishes its check/restore. `sync` probes YT Music auth up front. Browser cookies go stale within about an hour; after `auth ytmusic --from-browser` (records `ytmusic_browser` + `ytmusic_account` in config.json) `fetch_liked_songs` and `fetch_library_playlists` re-extract them once on a stale-looking failure (for the library, also an empty list followed by an unreadable account menu) and retries, in a candidate file that replaces browser.json only if the browser is still the recorded account (handle, else `<email sha256>|name` — never the photo URL, which is re-signed between sessions; else browser.json is untouched and the read fails) — another account's LM would make every check compare the wrong likes. An LM check fails only if the refresh or retry fails; every refresh failure is an `UnexpectedResponseError`.
- Quota: repoint 104 units, unlike_shadow 53, relike 103, canonical_repoint 104, canonical_merge 53 (restore ~51 and B-rollback ~51 not estimated). `--limit N` caps actions per run; scans have no `--limit`.
- Removed settings: `fuzzy_threshold` in config.json is ignored with a deprecation warning.
- Pre-alignment issue types exist only on old diagnosis rows; the code no longer produces or acts on them.
