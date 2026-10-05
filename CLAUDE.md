# CLAUDE.md

Project-specific instructions for like-surgeon. The global ~/.claude/CLAUDE.md still applies; this only adds project conventions.

## Documentation map

- [README.md](README.md) — user-facing install/usage, action-mapping table, caveats
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map, data flow, DB schema, design decisions
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — test strategy, fake/monkeypatch patterns, auth setup, debugging
- CLAUDE.md (this file) — conventions and gotchas that don't fit the above

## Domain model: the YT Music license-relink mechanism (read this first)

**This is the root reason like-surgeon exists.** Almost every finding type is a downstream symptom of one upstream behavior, so internalize it before touching `compare.py`, `diagnosis.py`, or `sync.py`.

The mechanism:

1. Song **A** is liked — `video_id A`, present in *both* your YouTube Liked Videos and your YT Music Liked Songs.
2. **A's license expires** (label/distributor pulls the upload, region restriction lands, etc.) → A becomes region-blocked / unplayable.
3. **YT Music auto-relinks**: it silently creates a *replacement* track **B** (a new, different `video_id`) for the same song, and **moves your YT Music like from A to B automatically**. You did not do this; YT Music did.
4. **YouTube does not get this update.** Your YouTube Liked Videos still holds the old like on **A**. The two platforms have now diverged for one song through no action of yours.

How that single event surfaces across `issue_type`s:

- If B still resembles A enough that the matcher links them — Stage 4 (same channel + duration ±2s + title) or the Stage-3 fuzzy matcher (`fuzzy_threshold`, default 85) → **`possible_pointer_drift`** (re-point the YouTube like A → B). Stage-4 pairs auto-sync, and so do fuzzy pairs that pass the same `videos.list` check; unverified fuzzy pairs need `sync --include-fuzzy-drift` (see sync-state notes).
- If B's title/artist drifted too far for the matcher → the relink is *missed* and B looks like a brand-new YT-Music-only like → **`ytmusic_only`**. So a large share of `ytmusic_only` is really *undetected pointer drift*, not "user liked it only in YT Music".
- A itself, now unplayable, surfaces as **`unavailable_video`** (unless `region_blocked`, which 0.7.1 deliberately leaves alone — see sync-state notes).
- "Resolving" a relink-origin `ytmusic_only` by liking B on YouTube (`yt_like`, 0.10) makes YT Music cross-prop B back into Liked Songs *alongside the entry it already had* → **`duplicate_in_source`**.

Consequence for design judgement: `possible_pointer_drift`, `ytmusic_only`, `unavailable_video`, and `duplicate_in_source` are frequently **four views of the same relinked song**, not independent problems. A change that "fixes" one bucket often just moves the song into another bucket. Always reason about the full A→B lifecycle, not the single finding in front of you.

**Attempted fix, PARKED — do not re-derive.** Auto-associating relink-origin `ytmusic_only` B with its dead `unavailable_video` original A (→ `possible_pointer_drift`, resolved by existing `yt_relike`) was researched/planned/reviewed twice (`.hyperclaude/`, slug `scoped-title-based-association-of`) and parked: title-only matching is an intrinsic false-positive magnet (a version-token vocabulary is unavoidable — must tell `(하루하루)` translation from `(Remix)` version decoration) and a wrong pair feeds destructive like-then-unlike drift sync. So **relink-origin `ytmusic_only` is manual-only** (user likes B / unlikes A on YouTube by hand). Don't revive title-only auto-association without solving false-positive containment first. The two YT-Music-write alternatives are also dead ends: `yt_like` self-reverts via `ytm_dedupe` (below); "2× YTM unlike + 1× YT like" is refuted — the relink-orphaned LM entry is sticky/unremovable via the YTM rating API.

## Releases

Cut a git tag **and** a GitHub release at every milestone bump (0.4.0, 0.4.1, 0.5.0, ...). Without these two artifacts there's no quick changelog and no rollback target — they're cheap, do them every time.

Procedure on `main`, after the feature PR is merged:

1. Bump `version` in `pyproject.toml` **and** `__version__` in `src/likesurgeon/__init__.py` — these two must stay in lockstep (v0.8.0 shipped with `__init__.py` stale at `0.7.1`, fixed in 0.9.0).
2. Commit `chore: bump version to <X.Y.Z>`.
3. Tag the commit `v<X.Y.Z>` (with the `v` prefix).
4. `git push && git push --tags`.
5. `gh release create v<X.Y.Z> --title "v<X.Y.Z> — <slug>" --notes "..."` — notes summarize what shipped (cribbing from the PR description is fine), include the action-mapping or quota table when behavior changed.

Don't retro-edit a published tag/release for follow-up fixes — cut a fresh patch tag instead.

## CI gate (local checks before push)

```bash
uv run pytest tests/
uv run ruff format --check .
uv run ruff check
```

CI runs all three. `ruff format --check` is the easy miss — `ruff check` alone catches lint but not formatting, and CI WILL fail on unformatted code.

## Workflow

Feature work goes through a PR (squash merge to main, see PR #7 for the template). One-line patches, chore commits (version bumps), README fixes, and lint corrections can go straight to main — solo-maintained, CI runs on every push.

## Sync state model (when touching sync.py / models)

- `DiagnosisItem.reason` is **never overwritten** — it's diagnosis-time evidence. Sync-side detail (errors, threshold notes, missing video_ids) goes on `SyncAttempt.reason`.
- `DiagnosisItem.status` is terminal in two cases: `'applied'` (code-set when every API call for the action succeeded, with one carve-out: `ytm_dedupe` is non-idempotent and flips to `'applied'` after any attempt regardless of outcome to prevent auto-retry over-removal) and `'skipped'` (either a manual override for findings the user never wants to retry, or code-set on cross-prop verify-miss — see `ytm_like` and `yt_like` bullets below). Anything else (`'open'`) gets re-evaluated next run. Status lives on a per-diagnosis row and every `compare-likes` writes fresh `'open'` rows, so `carry_over_skipped` copies `'skipped'` by `(issue_type, source_track_id, related_track_id)` from the most recent earlier diagnosis containing that finding (bridges gaps; an operator un-skip on the latest diagnosis wins) — without it, "terminal" only lasted until the next compare. `'applied'` is deliberately not carried (a finding that reappears after an applied fix didn't stick).
- Drift fixes are like → verify → unlike (fail-safe order). All three must succeed before flipping to `'applied'`. The `videos.getRating` verify (`yt_relike_verify`, shared `_verify_yt_liked` with `yt_like`) exists because of a live loss on 2026-10-05: for 2 of 38 relinks `rate(B, like)` returned 2xx but the rating never changed, and for one of them unliking A also removed B from YT Music — the LM entry for a relinked B rides on A's YouTube like (re-liking A restored B in LM instantly). Verify-miss → terminal `'skipped'`, A untouched. History fits: 6 of 117 earlier relikes went B 1→0 in LM.
- Continue-on-error is the dispatcher contract — the client boundaries wrap all exceptions as the typed errors the dispatcher catches (`YouTubeWriteError`; `YTMusicWriteError` for writes, `UnexpectedResponseError` / `AuthFileMissingError` for YT Music reads incl. the `ytm_like` verify) so the loop never aborts mid-run. An escaped exception rolls back the in-flight action's `SyncAttempt` rows — including writes that really happened. One deliberate exception: `YouTubeQuotaExceededError` (daily quota) stops all further YouTube actions after recording its failed row, since every later call would fail too (`ytm_dedupe` still runs). `ExecResult.left_unliked` names any `ytm_like` cut between its unlike and relike; the next `sync` re-detects these from attempt history (`stranded_unliked_video_ids`: last `ytm_like_yt_relike` per track failed) and runs their re-likes first.
- Before a scaled `sync` run (>~50 actions), re-scan with `scan youtube-likes` + `compare-likes`. **0.7.1**: `region_blocked` vids are no longer auto-unliked by the `unavailable_video` finding (`build_unavailable_video_items` skips them) — restrictions flip between days, and unliking a region-blocked vid would permanently lose the like when the restriction lifts (real example: 94 region-blocked ghosts un-blocked themselves overnight in testing, plus 4 false-positive unlikes during 0.7 live verification that had to be manually re-liked). Other `is_available=False` reasons (deleted / private / rejected / unavailable) still surface as ghosts.
- Duplicate dedupe (`ytm_dedupe`) is **one `rate_song(INDIFFERENT)` per finding, ytmusic-source + N=2 only**. `rate_song(LIKE)` is non-idempotent (every call appends to LM), so our 0.4 drift sync was the dup producer in the first place. Propagation is minute-scale; the hazard is running `scan ytmusic` → `compare-likes` → `sync` cycle before propagation settles (stale snapshot recreates the finding → over-remove). **Always re-scan both sources + compare-likes immediately before a dedupe sync** — a stale finding can also remove the only remaining LM entry if the dup was fixed manually or by propagation since the previous diagnosis, and the dedupe guard (below) reads YouTube likes from the YouTube scan, so a hand-made like after it is invisible. `sync` warns when a scan the diagnosis used is > 1h old (`sync_preflight.diagnosis_staleness`; warn-only by design).
- `ytm_like` cross-prop: verify-miss (video not found in YT Music after 5s) flips `DiagnosisItem.status` to `'skipped'` (code-set by `execute()`, not a manual override). This is intentional — prevents the 0.4-0.6 silent-no-op `rate_song("LIKE")` re-fire-on-every-sync noise. Operator can `likesurgeon unskip <id>` (latest diagnosis) to force retry. Partial failure: `rate(none)` succeeds but `rate(like)` fails (most likely at the quota boundary) — the video is left unliked on YouTube; `sync` prints it. Re-running `sync` on the same diagnosis re-likes it; re-scanning first loses it from the diff.
- **0.10**: `yt_like` reverse cross-prop (YT Music → YouTube direction): one `videos.rate("like")` (idempotent — no unlike-then-relike toggle required, unlike YTM's propagation path), then 5s wait + `videos.getRating` verify. Verify-miss flips `DiagnosisItem.status` to `'skipped'` (code-set by `execute()`, not a manual override) — typical cause is a region/license restriction preventing the like from landing. Operator can `likesurgeon unskip <id>` to force retry. ~51 quota units (50 rate + 1 getRating).
- **Pointer drift auto-sync is Stage-4 only by default.** `plan` keys eligibility on the reason prefix (`STAGE4_REASON_PREFIX` in `diagnosis.py`, shared with `_match_reason`). Stage-3 `token_set_ratio` returns 100 whenever one side's tokens are a subset of the other's, so `"Song"` vs `"Song (Remix)"` is confidence 1.0 and sails past `--drift-min-confidence`; `--include-fuzzy-drift` opts back in. Same false-positive class as the parked title-only association (domain-model section). **But** `compare-likes` re-verifies each fuzzy pair with Stage 4's own triple rule (`compare.verify_fuzzy_drift`: `videos.list` channel equal + duration ±2s + `normalize_for_match` title equal) and promotes passers to `STAGE4_ENRICHMENT` (reason `enriched (verified fuzzy N/100): …`). Motivation, from diagnosis #25 on the maintainer DB: all 31 fuzzy pairs at 1.0 were genuine relinks whose YTM *display* title gained a romanized suffix while the `videos.list` title stayed identical; the rule passed 28/37 with zero false positives, and rejected both real mis-pairs (0.83/0.85 — long shared artist names inflate `token_set_ratio` even for different songs). Misses were 3s duration gaps and `feat.`/`&` spelling diffs → left manual.
- **0.10 KNOWN LIMITATION — `yt_like` → `duplicate_in_source` → `ytm_dedupe` is a self-reverting no-op.** `yt_like` resolves a relink-origin `ytmusic_only` (likes B on YouTube), but YT Music cross-props B back into Liked Songs next to the entry it already had → `duplicate_in_source` (N=2). Deduping that with `ytm_dedupe`'s `rate_song("INDIFFERENT")` is *rating-level, not occurrence-level*: because the dup sits on a still-live cross-prop link, INDIFFERENT round-trips to YouTube and removes the `yt_like` like too, landing back at the original `ytmusic_only`. Net: `yt_like` then `ytm_dedupe` of the dup it spawned = quota spent, zero durable change. Historical-origin dups (0.4 `rate_song(LIKE)` append artifacts) were assumed not to have this (no live YouTube counterpart), but a 2026-10-05 audit of the maintainer DB contradicts that: all 12 past `ytm_dedupe` targets — including the 7 from 05-11/12 with no `yt_like` history — were in their diagnosis's YouTube snapshot (0.4 dups came from `possibly_missing_from_ytmusic`, i.e. YouTube-liked videos), and the YouTube like vanished for 4 of the 6 05-11 targets. Occurrence-level LM removal is not exposed by `ytmusicapi`, so a clean fix is an **open design problem** — do NOT `ytm_dedupe` a `yt_like`-induced dup expecting the YouTube like to survive. Verified live 5/5 reverted (2026-05-16). **Guarded in code:** `plan` skips `ytm_dedupe` for any video in `sync_preflight.youtube_liked_video_ids` (the diagnosis's YouTube snapshot ∪ videos a past `yt_like` / `yt_relike` liked; `None` = no YouTube scan → skip all dedupes) — the INDIFFERENT round-trip hits any YouTube-liked video, not just `yt_like`-induced ones, and without the guard scan→compare→sync cycles loop forever. Net effect: auto-dedupe only runs for dups whose video isn't liked on YouTube. Blind spot: a hand-made YouTube like after the YouTube scan (hence the scan-age warning); a live `videos.getRating` check per dedupe would close it at 1 unit each but makes dedupe-only runs need YouTube auth. `yt_relike`'s like half is the same `videos.rate("like")` on B, but history says it rarely dups: of 117 applied `yt_relike_like` (2026-05-10..14), B's LM count went 1→1 in 92 cases and 1→2 in only 4. This is the clearest concrete instance of the "four views of the same relinked song" trap from the domain-model section.
- **0.8**: one-time `scan youtube-likes` required to repopulate `artists` from `videoOwnerChannelTitle`. The **first** post-0.8 `compare-likes` will show a `metadata_drift` spike caused by the ingestion change itself; this spike won't clear on a re-run of `compare-likes` alone — verify it was migration noise by running a **second** `scan youtube-likes` + `compare-likes` so drift detection compares two post-0.8 snapshots.
- **0.9**: `fuzzy_threshold` config key (default `85`, integer in `[0, 100]`) tunes the cross-source RapidFuzz cutoff. Default is the safe choice — lowering raises `possible_pointer_drift` false-positive risk. Fuzzy drifts only reach drift sync (like-then-unlike) with `--include-fuzzy-drift`.
