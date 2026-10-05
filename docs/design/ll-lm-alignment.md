# Design: LL → LM alignment model (sync redesign)

Status: **r3 — implementation spec** (2026-10-05). r1 was the proposal; r2 added every fix from an adversarial review; r3 keeps the ones that guard against observed losses and drops the rest (deferred to dogfooding). Supersedes the relink handling in `CLAUDE.md` ("Domain model") and the 0.4–0.10 sync actions built on it.

## TL;DR

- **YouTube Liked videos (LL) is the only store of likes. YouTube Music Liked songs (LM) is a rendering of LL**: same order, filtered to what YT Music shows, each entry displayed as its playable track. A relinked song is not a like that moved from A to B — it is A's YouTube like *shown as* B.
- Because the order is preserved, **aligning the two lists by position recovers which YouTube like backs each LM entry** — no library-wide title matching.
- Inconsistencies reduce to a few cases on one data structure (the backing map), fixed by **two write actions** — *re-point* (like B → confirm → unlike A) and *unlike a shadow* — each followed by **one LM-wide check that re-likes A if LM doesn't look as expected**.
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
3. **Write eligibility**: only gaps with *k* = *m*, and only pairs that pass a sanity check — `videos.list` duration within ±3 s and (same channel or one normalized title contains the other). Everything else (*k* ≠ *m*, failed check, missing metadata) is **report-only**.

Output — the *backing map*: for each LM entry, its backing LL video (or none) and whether the pair is write-eligible; for each LL video, rendered as itself, as another `videoId`, or not rendered.

## 4. Findings and actions

For an LL video A rendered as B (availability from scan time):

| Finding | Condition | Action |
|---|---|---|
| `relinked` | A ≠ B, A unavailable, B not liked | **re-point** |
| `rendered_as_other` | A ≠ B, A playable, B not liked | report; re-point with `--include-playable` |
| `shadow_duplicate` | A ≠ B, B also liked (LM shows B twice) | **unlike shadow** if A unavailable; playable A only with `--include-playable` |
| `dead_unrendered` | A unavailable, not rendered | report (hold — a restriction can lift) |
| `unrendered_music` | A playable, looks like music, not rendered | report |
| `unbacked_lm_entry` | LM entry with no backing / not write-eligible | report, never act |
| `metadata_drift` | unchanged | — |

`--include-playable` acts on findings whose A is a playable video (MV, fan upload): it removes a real like the user made, so it is off by default; exclude individual findings with `skip`. Unknown availability (`is_available = None`) is report-only.

### 4.1 Actions

**re-point (A → B)** — 102 units
1. `videos.rate(B, like)`; wait 5 s; `getRating(B)` must be `like`, else stop (A untouched).
2. `videos.rate(A, none)`; wait 5 s; `getRating(A)` must be `none`.

**unlike shadow (A behind B)** — 51 units
1. `getRating(B)` must be `like`; `videos.rate(A, none)`; wait 5 s; `getRating(A)` must be `none`.

**Post-action LM check (both actions).** Read the full LM before and after the action (the "after" read doubles as the next action's "before"). Expected: re-point → LM unchanged; unlike shadow → one fewer B, B still ≥ 1. If LM doesn't match (re-read once after 15 s), **re-like A** and confirm with `getRating`; if the restore can't run (quota, auth), record it as stranded (existing `stranded_unliked_video_ids` reporting).

**Never** call `rate_song(INDIFFERENT)` or `rate_song(LIKE)` on YT Music.

## 5. Operational rules

1. **Fresh, unbroken scans.** `compare-likes` refuses to plan writes when one of our own `sync` attempts happened between the two scans it uses (scans that straddle a write misalign). The existing stale-diagnosis warning stays.
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
3. Does an un-like/re-like make YT Music render an `unrendered_music` video (the old `ytm_like` premise)?
4. Ordering on other accounts (one J-pop/anime-heavy account so far).

## 8. Rollout

1. Implement (§6) in one pass with the implement loop; synthetic tests for `align.py`.
2. Dogfood on the live account: the two remaining LM duplicates (トゥインクル☆スター ← `IKTlJwxnu4o`, 好き、泣いちゃいそうだ (Acoustic) ← `LPEE4MMDieo`; both playable → `--include-playable`). Remaining default paths (`relinked`) get exercised as new relinks appear.
3. Release as a minor version (tag + GitHub release per `CLAUDE.md`).

## Appendix: artifacts

`.hyperclaude/research/`: `20261005-ll-lm-alignment.py` (greedy prototype, superseded by LIS), `20261005-relink-{inventory,cleanup}.py` + `…-cleanup-log.json`, `20261005-review-replay/` (review scripts: LIS replay, ordering baselines, duplicate-anchor and straddling cases).
