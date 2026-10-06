# CLAUDE.md

Project-specific instructions for like-surgeon. The global ~/.claude/CLAUDE.md still applies; this only adds project conventions.

## Documentation map

- [README.md](README.md) — user-facing install/usage, findings table, sync actions, caveats
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map, data flow, DB schema, design decisions
- [docs/design/ll-lm-alignment.md](docs/design/ll-lm-alignment.md) — the LL→LM alignment spec (domain model, findings, actions)
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — test strategy, fake/monkeypatch patterns, auth setup, debugging
- CLAUDE.md (this file) — conventions and gotchas that don't fit the above

## Domain model: LL is the store, LM renders it (read this first)

Full spec: [docs/design/ll-lm-alignment.md](docs/design/ll-lm-alignment.md). Internalize this before touching `align.py`, `diagnosis.py`, or `sync*.py`.

- **YouTube Liked videos (LL) is the only store of likes.** YouTube Music Liked songs (LM) is a *rendering* of LL: same order, filtered to what YT Music shows, each entry displayed as a playable track whose `videoId` (B) can differ from the YouTube video (A) whose like backs it.
- A relink is not a like that moved from A to B — it is A's like *shown as* B. Official MVs / fan uploads shown as their audio track ("shadows"), re-uploads, and duplicates follow from the same thing. If B is also liked on YouTube, LM shows B twice (`shadow_duplicate`).
- LM exposes no backing id, so **order is the only signal**: `align.py` anchors LM entries whose `videoId` is itself in LL (LIS), then pairs the gaps in order. Only gaps with equal counts (*k* = *m*) are paired. Pairs are write-eligible (confidence 1.0) when A's availability is known and — except `shadow_duplicate` — they pass the same-recording check (`videos.list` duration within ±3 s and same channel or overlapping titles). Shadows skip that check because they are different uploads by nature; `unlike_shadow` proves the pair at run time (B shown ≥2× before, exactly one B fewer after, else restore). Everything else is report-only.
- Two write actions, both on YouTube only: **repoint** (like B, confirm, unlike A) and **unlike_shadow** (B liked and shown ≥2×: unlike A). Each is followed by one LM-wide check; a mismatch re-likes A (restore). Unliking a wrong A would remove an unrelated LM entry, which is why the check exists.

**Rules.** Never call YT Music `rate_song` (same rating as YouTube's; it only duplicates or reverts) — sole exception: unliking a *deleted* video by hand, which `videos.rate` 404s on (README, Compare across sources). Never match by title/fuzzy. Never re-add `yt_like` / `ytm_dedupe` — removed; see the spec. Safety invariants: see 'Never / always' under Sync state model — read them before touching `sync*.py`.

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

## Sync state model (when touching sync*.py / models)

**Never / always**
- Never overwrite `DiagnosisItem.reason` (diagnosis-time evidence); sync detail goes on `SyncAttempt.reason`.
- Always commit each write's `SyncAttempt` row immediately.
- Never unlike A without `getRating` confirmation, a full-LM check, and a restore (re-like A) on mismatch.
- Never act on unknown availability (`is_available` None).
- `sync` must refuse (exit 1) if any non-skipped sync attempt happened after the older of the diagnosis's two scans; `compare-likes` warns on the same condition.

**Operational notes**
- Planner (`sync.plan`, pure): write-eligible pair findings become `repoint` / `unlike_shadow`; others become planner skips (audit row only, item stays `'open'`). `--include-playable` (off by default) also acts on a playable A (MV / fan upload), which removes a like the user made.
- Dispatcher (`sync_dispatch.execute`): LM check is repoint = unchanged, unlike_shadow = one B fewer (B still ≥1), one 15 s re-read on mismatch. A still liked after its unlike is re-liked first. A failed restore means A is stranded.
- **Stranded** = A unliked and its re-like failed/unconfirmed, or the action was interrupted after the unlike (`interrupted` row, also on Ctrl-C). `stranded_unliked_video_ids` (newest settling row per track) lists them on every `sync` run until a later YouTube scan contains the video.
- `DiagnosisItem.status`: `'applied'` only when every call succeeded and the LM check passed. `'skipped'` is terminal (B's like didn't land, precheck failed, A restored, or `skip`); `unskip` re-opens; it carries over by `(issue_type, source_track, related_track)` (`carry_over_skipped`). Anything else stays `'open'`.
- Both actions precheck A is still liked (stale → `skipped`, no writes); a repoint also requires B not yet liked (else stale → `skipped`) and, whenever A stays liked after B's like may have landed (restore, quota-rejected unlike, B's like erroring), unlikes B (`repoint_rollback*`; unconfirmed → `ExecResult.left_liked`). Any action needing a restore, successful or not, stops the run (`aborted`, re-scan + `compare-likes` needed). LM unreadable before an action stops the run (`aborted`); quota exhaustion stops it after the in-flight action finishes its check/restore. `sync` probes YT Music auth up front; browser cookies went stale within about an hour in practice, so re-run `auth ytmusic --from-browser` right before scanning if a scan reports a logged-out response.
- Quota: repoint 104 units, unlike_shadow 53 (restore ~51 and B-rollback ~51 not estimated). `--limit N` caps actions per run; scans have no `--limit`.
- Removed settings: `fuzzy_threshold` in config.json is ignored with a deprecation warning.
- Pre-alignment issue types exist only on old diagnosis rows; the code no longer produces or acts on them.
