# Design: LL → LM alignment model (sync redesign)

Status: **implemented in 0.11.0** — spec r3 (2026-10-05). r1 was the proposal; r2 added every fix from an adversarial review; r3 keeps the ones that guard against observed losses and drops the rest (deferred to dogfooding). Supersedes the relink handling in `CLAUDE.md` ("Domain model") and the 0.4–0.10 sync actions built on it.

## TL;DR

- **YouTube Liked videos (LL) is the only store of likes. YouTube Music Liked songs (LM) is a rendering of LL**: same order, filtered to what YT Music shows, each entry displayed as its playable track. A relinked song is not a like that moved from A to B — it is A's YouTube like *shown as* B.
- Because the order is preserved, **aligning the two lists by position recovers which YouTube like backs each LM entry** — no library-wide title matching.
- Inconsistencies reduce to a few cases on one data structure (the backing map), fixed by **write actions** — *re-point* (like B → confirm → unlike A), *unlike a shadow* and, opt-in, `relike` of an unrendered like — each followed by **one LM-wide check that re-likes A if LM doesn't look as expected** (a `relike` never restores; it only stops the run).
- Replaces the matcher stages (fuzzy / canonical / Stage 4), `yt_like`, `ytm_dedupe`, the `ytm_like` toggle, and the gates/guards added to contain them.

## 1. Why the current model fails

`CLAUDE.md` models a relink as: A's license expires → YT Music **moves** the like to a replacement B → YouTube never hears about it. Each rule built on that needed a patch: `yt_like` creates LM duplicates; `ytm_dedupe` (`INDIFFERENT` on B) undoes `yt_like`; the "orphaned" LM entry can't be removed; the fuzzy matcher pairs version variants at 1.0; 0.7.1 hid region-blocked videos from findings; a relike lost a song from both platforms.

Under the rendering model none of these are quirks: `yt_like` adds a *second* like rendering as B (duplicate); `INDIFFERENT` on B removes B's like, not A's (self-revert); the orphaned entry is A's like and disappears when A is unliked; and the 0.7.1 exclusion hid exactly the A's that needed fixing.

## 2. The model

```
LL (YouTube ratings, like-time order)          LM (YT Music Liked songs, same order)
  v1  ok        ─────────────────────────────▶   v1
  v2  vlog      ✗ not rendered
  A3  region-blocked ──── rendered as ──────▶   B3   ← relink: A3's like shown as B3
  v4  ok        ─────────────────────────────▶   v4
  M5  official MV ─────── rendered as ──────▶   T5   ← MV like shown as its audio track
  T5  ok        ─────────────────────────────▶   T5   ← T5 also liked → LM shows T5 twice
```

1. Each LM entry is backed by one LL like; LM keeps LL's order; re-liking moves a video to the top of both.
2. The backing video may differ from the displayed `videoId`: region-blocked/expired originals, official MVs, fan uploads, re-uploads, a making-of video were all observed.
3. LM exposes no backing id (`feedbackTokens` are opaque), so **order is the only direct signal**.
4. YT Music's `rate_song(LIKE)` creates a YouTube like on that video — it's the same rating.

**Evidence** (one account; read-only DB checks + live writes, 2026-10-05):
- LL#46/LM#45: 1142/1142 non-duplicate LM entries are in LL order (a shuffled list keeps ≈62).
- Live cleanup (`.hyperclaude/research/20261005-relink-cleanup-log.json`): 3 re-points observed B 1→2→1 in LM, 17 more verified by end state after rescan, 7 shadow unlikes each removed exactly one LM entry. Order alignment matched 23/24 write-verified pairs; the title-based inventory mis-attributed one that alignment got right.
- Failure mode guarded below: the old relike reported 2xx for both calls, yet on 2/38 pairs a write didn't take effect — once the unlike landed without the like and the song vanished from LM until A was re-liked by hand.

## 3. Alignment

Pure function over two complete snapshots `LL[0..n)` and `LM[0..k)`, newest first.

1. **Anchors**: LM entries whose `videoId` is itself in LL, restricted to a **maximum-length strictly increasing subsequence** of their LL indices (LIS; a greedy pass collapses on real data). When an LL video has several LM copies that fit the same anchor window, anchor the copy that leaves the fewest unbacked LM entries in the two adjacent gaps; ties go to the earliest copy. (Whether B's own like is newer or older than the shadow A decides which copy is B's own, so neither "earliest" nor "latest" is right on its own.)
2. **Gaps**: between consecutive anchors, the non-anchor LM entries (*k*) are rendered from the unanchored LL videos in the same gap (*m*), in order.
3. **Write eligibility**: only gaps with *k* = *m*, and only pairs that pass a sanity check — `videos.list` duration within ±3 s and (same channel or one normalized title contains the other). Everything else (*k* ≠ *m*, failed check, missing metadata) is **report-only**. Exception (dogfooding, 2026-10-06): `shadow_duplicate` skips the sanity check — a shadow is a different upload by nature (MV, fan or making-of video), and `unlike_shadow` verifies the pair directly (B shown ≥2× before, exactly one B fewer after, else A is re-liked).

Output — the *backing map*: for each LM entry, its backing LL video (or none) and whether the pair is write-eligible; for each LL video, rendered as itself, as another `videoId`, or not rendered.

## 4. Findings and actions

For an LL video A rendered as B (availability from scan time):

| Finding | Condition | Action |
|---|---|---|
| `relinked` | A ≠ B, A unavailable, B not liked | **re-point** |
| `rendered_as_other` | A ≠ B, A playable, B not liked | report; re-point with `--include-playable` |
| `shadow_duplicate` | A ≠ B, B also liked (LM shows B twice) | **unlike shadow** if A unavailable; playable A only with `--include-playable` |
| `dead_unrendered` | A unavailable, not rendered | report (hold — a restriction can lift) |
| `unrendered_music` | A playable, looks like music, not rendered | report; **`relike`** with `--relike-unrendered` |
| `unbacked_lm_entry` | LM entry with no backing / not write-eligible | report, never act |
| `metadata_drift` | unchanged | — |

`--include-playable` acts on findings whose A is a playable video (MV, fan upload): it removes a real like the user made, so it is off by default; exclude individual findings with `skip`. Unknown availability (`is_available = None`) is report-only.

### 4.1 Actions

**re-point (A → B)** — 104 units
0. `getRating(A)` must be `like` (else skip, no writes); `getRating(B)` must be `none` (B already liked → stale, skip, no writes).
1. `videos.rate(B, like)`; wait 5 s; `getRating(B)` must be `like`, else stop (A untouched).
2. `videos.rate(A, none)`; wait 5 s; `getRating(A)` must be `none`.

**unlike shadow (A behind B)** — 53 units
1. `getRating(B)` must be `like`, `getRating(A)` must be `like` and LM must currently show B at least twice; `videos.rate(A, none)`; wait 5 s; `getRating(A)` must be `none`.

**`relike` (A)** — 103 units (`--relike-unrendered`; `unrendered_music` only)
0. `getRating(A)` must be `like` (else skip, no writes).
1. `videos.rate(A, none)`; `getRating(A)` must be `none`.
2. `videos.rate(A, like)`; `getRating(A)` must be `like`, tried twice if unconfirmed.
3. LM check (below). If the unlike wasn't confirmed, a `relike_unlike_pending` row is committed before the check and A gets one recovery re-like after it, because a late unlike would otherwise go unnoticed (an unrendered A gives the check nothing to catch).

Expected LM: nothing removed, at most one entry added. +1 → applied; no change (one 15 s re-read first) → skipped, terminal, when every call was clean; anything removed or more than one entry added, or an unconfirmed unlike or re-like, stops the run with A liked (or reported stranded); an unlike call that errored but landed just ends `failed`. Oldest first, because re-liking moves the video to the top of both lists.

**Post-action LM check (all actions).** Read the full LM before and after the action (the "after" read doubles as the next action's "before"). Expected: re-point → LM unchanged; unlike shadow → one fewer B, B still ≥ 1; `relike` → nothing removed, at most one entry added (it never counts as a restore: a failed check stops the run with A liked). If LM doesn't match (re-read once after 15 s), **re-like A (restore)** and confirm with `getRating`; if the restore can't run (quota, auth), record it as stranded (existing `stranded_unliked_video_ids` reporting). A re-point that leaves A liked after B's like may have landed (restore, quota-rejected unlike, B's like erroring) also unlikes B (`rate(B, none)` + `getRating`, failure reported as `left_liked`). Any action needing a restore, successful or not, stops the run: the mismatch means an order-derived pair was wrong, so later pairs may be too — re-scan and re-run `compare-likes`.

**Never** call `rate_song(INDIFFERENT)` or `rate_song(LIKE)` on YT Music. (Outside `sync`, `rate_song(INDIFFERENT)` is the only way to unlike a *deleted* LL video — `videos.rate` 404s on it; verified 2026-10-06. Deleted means unrendered, so there is no LM entry to revert.)

## 5. Operational rules

1. **Scans newer than our last write.** `sync` refuses to write when one of our own `sync` attempts happened after the older of the two scans its diagnosis uses — a write between the scans misaligns them, and a write after both makes the diagnosis describe lists that no longer exist. `compare-likes` warns on the same condition. The existing stale-diagnosis warning stays.
2. **No partial scans.** Remove `--limit` from both `scan` commands; alignment of a truncated scan is meaningless.
3. **Quota / auth.** Existing quota stop and stranded reporting; YT Music auth probe before sync (cookies from Chrome went stale within about an hour in testing).

## 6. Code impact

| Area | Change |
|---|---|
| New `align.py` | pure alignment → backing map; unit tests with small synthetic lists (consecutive relinks, duplicate copies, edge gaps, *k* ≠ *m*) |
| `compare.py` | stages 1–4, fuzzy, `verify_fuzzy_drift` removed; **keep `dedupe_by_video_id`** (`metadata_drift` uses it) |
| `diagnosis.py` | new issue types; `carry_over_skipped` unchanged (old-type skips simply don't carry); the evidence-in-`reason` invariant stays |
| `sync.py` | planner → `repoint` / `unlike_shadow`; dispatcher keeps continue-on-error, quota stop, `SyncAttempt` per call, verify helpers; adds the post-action LM check + restore. Removed: `yt_like`, `ytm_dedupe`, `ytm_like`, fuzzy gate, dedupe guard |
| `doctor.py` | match rate and `DiagnosisSummary` rebuilt on the new finding types (today they hard-code the old six) |
| `cli.py` | `compare-likes` runs alignment + the straddle check; `sync` loses `--include-fuzzy-drift` / `--drift-min-confidence`, gains `--include-playable`; `scan --limit` removed; `skip`/`unskip` kept |
| Config | `fuzzy_threshold` parsed but ignored, with a deprecation warning |
| Docs | `CLAUDE.md` domain model rewritten around §2; README action table and sync docs; ARCHITECTURE; DEVELOPMENT (`--limit` tips) |

Old diagnoses stay readable; old issue types exist only on old rows. No schema change.

## 7. Open questions (answer by dogfooding)

1. Does the LL API stop at 5000 likes? If so, alignment must bound itself to the overlapping range.
2. Do renders of unavailable videos flicker between reads? Seen twice, both right after nearby writes; the post-action check covers it if real.
3. Does an un-like/re-like make YT Music render an `unrendered_music` video (the old `ytm_like` premise)? **Mostly yes** (2026-10-06): all 26 unrendered LL videos were un-liked and re-liked on YouTube, oldest first, each step confirmed by `getRating`; LM lost nothing and gained 18 — 13 rendered as themselves, 5 as an official track (another `videoId`). The 8 still unrendered: 6 memes / general videos and 2 game-BGM uploads. Two caveats: the music heuristic had classified all 26 as non-music (covers, 歌ってみた, OST arrangements), so none had been an `unrendered_music` finding; and the 5 official-track renders came back as 2 `rendered_as_other` (same-recording check failed: other channel, title in another script) plus 3 `unbacked_lm_entry`, because re-liked videos that stayed unrendered sit in the same gaps (*k* ≠ *m*). 0.12 ships this as `sync --relike-unrendered`, and the music heuristic now covers the families that were missed (covers, 歌ってみた, soundtracks, remasters, remixes and more).
4. Ordering on other accounts (one J-pop/anime-heavy account so far).

## 8. Rollout

1. Implement (§6) in one pass with the implement loop; synthetic tests for `align.py`.
2. Dogfood on a real account: the two LM duplicates left there (both playable → `--include-playable`). Remaining default paths (`relinked`) get exercised as new relinks appear.
3. Release as a minor version (tag + GitHub release per `CLAUDE.md`).

## Appendix: artifacts

`.hyperclaude/research/`: `20261005-ll-lm-alignment.py` (greedy prototype, superseded by LIS), `20261005-relink-{inventory,cleanup}.py` + `…-cleanup-log.json`, `20261005-review-replay/` (review scripts: LIS replay, ordering baselines, duplicate-anchor and straddling cases).
