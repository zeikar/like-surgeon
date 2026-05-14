# CLAUDE.md

Project-specific instructions for like-surgeon. The global ~/.claude/CLAUDE.md still applies; this only adds project conventions.

## Documentation map

- [README.md](README.md) — user-facing install/usage, action-mapping table, caveats
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map, data flow, DB schema, design decisions
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — test strategy, fake/monkeypatch patterns, auth setup, debugging
- CLAUDE.md (this file) — conventions and gotchas that don't fit the above

## Releases

Cut a git tag **and** a GitHub release at every milestone bump (0.4.0, 0.4.1, 0.5.0, ...). Without these two artifacts there's no quick changelog and no rollback target — they're cheap, do them every time.

Procedure on `main`, after the feature PR is merged:

1. Bump `version` in `pyproject.toml` AND `__version__` in `src/likesurgeon/__init__.py` (must stay in lockstep — v0.8.0 shipped with `__init__.py` stale at `0.7.1`, fixed in 0.9.0).
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
- `DiagnosisItem.status` is terminal in two cases: `'applied'` (code-set when every API call for the action succeeded, with one carve-out: `ytm_dedupe` is non-idempotent and flips to `'applied'` after any attempt regardless of outcome to prevent auto-retry over-removal) and `'skipped'` (manual override for findings the user never wants to retry). Anything else (`'open'`) gets re-evaluated next run.
- Drift fixes are like-then-unlike (fail-safe order). Both halves must succeed before flipping to `'applied'`.
- Continue-on-error is the dispatcher contract — `rate_video` / `like_song` wrap all exceptions as typed write errors so the loop never aborts mid-run.
- Before a scaled `sync` run (>~50 actions), re-scan with `scan youtube-likes` + `compare-likes`. **0.7.1**: `region_blocked` vids are no longer auto-unliked by the `unavailable_video` finding (`build_unavailable_video_items` skips them) — restrictions flip between days, and unliking a region-blocked vid would permanently lose the like when the restriction lifts (real example: 94 region-blocked ghosts un-blocked themselves overnight in testing, plus 4 false-positive unlikes during 0.7 live verification that had to be manually re-liked). Other `is_available=False` reasons (deleted / private / rejected / unavailable) still surface as ghosts.
- Duplicate dedupe (`ytm_dedupe`) is **one `rate_song(INDIFFERENT)` per finding, ytmusic-source + N=2 only**. `rate_song(LIKE)` is non-idempotent (every call appends to LM), so our 0.4 drift sync was the dup producer in the first place. Propagation is minute-scale; the hazard is running `scan ytmusic` → `compare-likes` → `sync` cycle before propagation settles (stale snapshot recreates the finding → over-remove). **Always re-scan ytmusic + compare-likes immediately before a dedupe sync** — a stale finding can also remove the only remaining LM entry if the dup was fixed manually or by propagation since the previous diagnosis.
- `ytm_like` cross-prop: verify-miss (video not found in YT Music after 5s) flips `DiagnosisItem.status` to `'skipped'` (code-set by `execute()`, not a manual override). This is intentional — prevents the 0.4-0.6 silent-no-op `rate_song("LIKE")` re-fire-on-every-sync noise. Operator can flip status to `'open'` via SQL (`UPDATE diagnosis_items SET status='open' WHERE ...`) to force retry. Rare partial-failure: `rate(none)` succeeds but `rate(like)` fails after client-level retries — the video is left unliked on YouTube. Manual relike required to recover.
- **0.8**: one-time `scan youtube-likes` required to repopulate `artists` from `videoOwnerChannelTitle`. The **first** post-0.8 `compare-likes` will show a `metadata_drift` spike caused by the ingestion change itself; this spike won't clear on a re-run of `compare-likes` alone — verify it was migration noise by running a **second** `scan youtube-likes` + `compare-likes` so drift detection compares two post-0.8 snapshots.
- **0.9**: `fuzzy_threshold` config key (default `85`, integer in `[0, 100]`) tunes the cross-source RapidFuzz cutoff. Default is the safe choice — lowering raises `possible_pointer_drift` false-positive risk, which feeds drift sync (like-then-unlike, fail-safe ordering limits but does not eliminate mis-match harm).
