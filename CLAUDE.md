# CLAUDE.md

Project-specific instructions for like-surgeon. The global ~/.claude/CLAUDE.md still applies; this only adds project conventions.

## Releases

Cut a git tag **and** a GitHub release at every milestone bump (0.4.0, 0.4.1, 0.5.0, ...). Without these two artifacts there's no quick changelog and no rollback target — they're cheap, do them every time.

Procedure on `main`, after the feature PR is merged:

1. Bump `version` in `pyproject.toml` (semver).
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

## Sync state model (when touching sync.py / models)

- `DiagnosisItem.reason` is **never overwritten** — it's diagnosis-time evidence. Sync-side detail (errors, threshold notes, missing video_ids) goes on `SyncAttempt.reason`.
- `DiagnosisItem.status` is terminal in two cases: `'applied'` (code-set when every API call for the action succeeded) and `'skipped'` (manual override for findings the user never wants to retry). Anything else (`'open'`) gets re-evaluated next run.
- Drift fixes are like-then-unlike (fail-safe order). Both halves must succeed before flipping to `'applied'`.
- Continue-on-error is the dispatcher contract — `rate_video` / `like_song` wrap all exceptions as typed write errors so the loop never aborts mid-run.
