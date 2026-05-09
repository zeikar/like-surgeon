# likesurgeon 0.3 — Matching Engine Design

**Status:** Approved (brainstorming complete) — feeds into writing-plans for implementation.

**Goal.** Add two diagnosis dimensions on top of the existing cross-source matcher:

1. **Ghost detection** — flag YouTube likes whose video is no longer playable (deleted, rejected, made private).
2. **Drift detection** — flag tracks whose metadata (title or artists list) changed meaningfully between two snapshots of the same source.

Both feed into the existing `Diagnosis` / `DiagnosisItem` pipeline so `compare-likes` remains the single entry point and `issues` / `doctor` work without special-casing.

---

## Scope

**In scope (0.3):**
- New columns on `SnapshotItem`: `is_available: bool | None`, `unavailable_reason: str | None`.
- `videos.list` status check folded into `scan youtube-likes`.
- `drift.py` module + `compare-likes` integration to compare each source's latest+previous snapshot pair.
- Two new `issue_type` strings: `unavailable_video`, `metadata_drift`.
- Doctor counts and README updates.

**Out of scope / deferred:**
- Region-block detection (`regionRestriction.blocked`) — needs user-region input we don't have. Defer to 0.3.x if demand surfaces.
- Age-restricted detection — content still plays for signed-in adults; not strictly a "ghost".
- `unlisted` videos — user has the link (it's in their LL), so still playable.
- Ghost detection on YT Music — high false-positive rate against YouTube Data API (Topic auto-tracks, YTM-only gated videos).
- Alembic / proper migration tooling. 0.3 ships a tiny **inline additive migration** instead — see the "Schema migration" subsection below. Alembic stays rejected at MVP scope.
- Tunable drift threshold via CLI flag — keep 90 as a constant; revisit if real data argues otherwise.
- Modifying the doctor "match-rate health score" formula. New counts are reported as separate rows; the score remains cross-source-match-rate-only.

---

## Architecture

```
┌───────────────────────────────┐
│  scan youtube-likes           │
│   ├─ playlistItems.list       │
│   └─ videos.list (NEW)        │  ← writes is_available / unavailable_reason
└──────────┬────────────────────┘
           ▼
   SnapshotItem rows
           │
           ▼
┌───────────────────────────────┐
│  compare-likes                │
│   ├─ cross-source matcher     │  (existing)
│   ├─ ghost finder      (NEW)  │  reads is_available=False rows
│   └─ drift finder      (NEW)  │  reads (latest, prev) snapshot pair
└──────────┬────────────────────┘
           ▼
   DiagnosisItem rows (issue_type ∈
     {possibly_missing_from_ytmusic,
      possible_pointer_drift,
      ytmusic_only,
      unavailable_video,         (NEW)
      metadata_drift})           (NEW)
           │
           ▼
┌──────────────────┬────────────┐
│  doctor (counts) │  issues    │
│   +unavailable   │   --type   │
│   +drift         │   filters  │
└──────────────────┴────────────┘
```

---

## Component 1 — Ghost path (scan-time)

### YouTube Data API call

New method on `YouTubeClient`:

```python
def fetch_video_statuses(self, video_ids: list[str]) -> dict[str, VideoStatus]:
    """Batch videos.list?part=status with up to 50 IDs per call.

    IDs missing from the response are returned as the placeholder
    VideoStatus(is_available=False, reason='missing_from_videos_list').
    This is explicitly a *placeholder*: this function takes only video
    IDs and has no access to playlistItems.list snippet titles, so it
    cannot tell "made private" from "deleted" from "region-restricted".
    The scan command resolves the placeholder into a final reason by
    cross-referencing each item's snippet title (see "Status mapping
    pipeline" below) before persisting. The placeholder must never
    reach the database.

    On batch failures (network / 5xx / quota — see "Batch failure
    policy" below) every ID in the failed batch is returned as
    VideoStatus(is_available=None, reason='status_check_failed').
    The caller always gets one entry per input ID.
    """
```

`VideoStatus` is a frozen dataclass: `is_available: bool | None`, `reason: str | None`. The `None` value of `is_available` mirrors the `SnapshotItem.is_available` column's tri-state ("available" / "unavailable" / "unknown") so the unknown case is modelled uniformly rather than by absence-from-dict at the call site.

**Quota.** `videos.list` is 1 unit per call. 5000 likes → 100 calls = 100 units, on top of the existing ~100 units for `playlistItems.list`. Total scan ≈ 200 units; daily quota is 10K.

### Status mapping pipeline

Status determination happens in **two stages** because the `videos.list` response alone can't distinguish "deleted" from "made private (caller-not-owner)" — both manifest as "ID absent from response". The snippet title YouTube returns inside `playlistItems.list` is what disambiguates them, and that data lives at the scan-command call site, not inside `fetch_video_statuses`.

**Stage 1 — `fetch_video_statuses` (videos.list-only):**

| Input from `videos.list` | `is_available` | `reason` |
|---|---|---|
| `status.uploadStatus = "rejected"` | `False` | `"rejected"` |
| `status.uploadStatus = "deleted"` | `False` | `"deleted"` |
| anything else (item *present* in response) | `True` | `None` |
| ID *absent* from response | `False` | `"missing_from_videos_list"` *(placeholder for stage 2)* |

Note that `status.privacyStatus = "private"` is **not** an unavailable signal. A `videos.list` call only returns a private video when the authenticated caller is the owner (or has been granted explicit access) — caller-not-owner private videos are silently omitted from the response and end up in the placeholder path instead. So a returned private video is, by definition, playable for *this* user; treating it as a ghost would be a false positive. `unlisted` is similar — returned to anyone with the link, and the user has the link by virtue of the video sitting in their LL.

**Stage 2 — scan-command disambiguator (`scan youtube-likes`, after fetch returns):**

For each item where stage 1 left `reason == "missing_from_videos_list"`, look up the corresponding `playlistItems.list` snippet title and rewrite the `VideoStatus`:

| Snippet title | Final `reason` |
|---|---|
| `"Private video"` (caller-not-owner private — YouTube's documented placeholder) | `"private"` |
| `"Deleted video"` (removed — same documented placeholder pattern) | `"deleted"` |
| anything else (region/age-restricted, weird API state, etc.) | `"unavailable"` *(conservative)* |

Why split: cross-referencing the snippet title prevents conflating "made private" (the user's actual diagnostic concern — they may want to ask the owner, find an alternative upload, etc.) with "deleted" (gone for everyone — different recovery path). Region/age-restricted videos legitimately exist in the user's library while being gated by client context, so the conservative `"unavailable"` keeps them visible without false-tagging.

The `"missing_from_videos_list"` placeholder NEVER reaches the database. It exists only as the handoff between stages 1 and 2; if the scan command ever sees it post-disambiguation that's an implementation bug.

### Batch failure policy

`videos.list` returns an HTTP 200 with a possibly-shorter `items` list when individual IDs in the batch are deleted (the API silently omits them — that's how we detect deletion). The genuine failure modes for a 50-ID batch are:

| Failure | Policy |
|---|---|
| Network error / HTTP 5xx | Retry the same batch with exponential backoff (e.g. 2 retries at ~1s and ~3s). If still failing, mark **only this batch's** items as `is_available=None, unavailable_reason="status_check_failed"`. |
| HTTP 403 `quotaExceeded` (or `dailyLimitExceeded`) | Bail the entire status-check phase — no point retrying within the same scan. Mark this and all *remaining* batches' items as `is_available=None, unavailable_reason="status_check_failed"`. |
| HTTP 404 `videoNotFound`, **batch size = 1** (per the [official `videos.list` errors table](https://developers.google.com/youtube/v3/docs/videos/list#errors), `videoNotFound` only surfaces in this case — multi-ID calls return 200 with bogus/deleted IDs silently omitted) | Persist that one ID as `VideoStatus(is_available=False, reason="deleted")`. A bogus ID and a deleted video are functionally indistinguishable for our purposes. |
| HTTP 404 on **batch size > 1** (defensive — not the documented behaviour but treat as theoretically possible) | Persist *all* IDs in the batch as `VideoStatus(is_available=None, reason="status_check_failed")`. Do NOT binary-split or per-ID-retry to isolate a bad ID — the cost (up to 50 extra requests, quota burn, complexity) outweighs the under-detection of one undocumented edge case. The next scan retries cleanly. |
| Unexpected exception (other 4xx including 401/400, raw transport errors, malformed JSON) | Persist batch IDs as `VideoStatus(is_available=None, reason="status_check_failed")`. Re-raise only if it would corrupt the surrounding scan transaction. |

We deliberately do **not** binary-split or per-ID-retry on partial failures. Per-ID 404s are already handled by the silent-omission path; for the remaining failure modes, smaller calls don't help (the same network/quota condition applies) and 50× the request count would just burn quota faster.

In all cases, scan does NOT abort because of status-check failure — the playlist data is still useful and a later re-scan picks up the truth.

### Schema

`SnapshotItem` gains two nullable columns:

```python
is_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
unavailable_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

For new installs, `Base.metadata.create_all(engine)` creates the table with the columns from the start. For 0.2.x DBs being upgraded in place, the inline migration below adds the columns; existing rows (snapshots taken before 0.3) end up with both fields `NULL` and are interpreted as "unknown" — they don't surface as ghosts. Without the inline migration, SQLAlchemy queries against the new mapped columns would fail at runtime against an unmigrated table, so the migration step is load-bearing rather than a nice-to-have.

### Schema migration

SQLite supports `ALTER TABLE … ADD COLUMN` for nullable columns without rewriting existing rows. 0.3 ships a small idempotent helper invoked at engine construction time (so every CLI command transparently bootstraps the schema):

```python
def _migrate_in_place(engine: Engine) -> None:
    """Idempotently add columns introduced after a table was first created.

    Skips tables that don't exist yet (fresh install — `init_db` hasn't run).
    Safe to call on every engine construction.
    """
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "snapshot_items" not in tables:
            return
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(snapshot_items)")
        }
        for name, decl in (("is_available", "BOOLEAN"), ("unavailable_reason", "VARCHAR(32)")):
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE snapshot_items ADD COLUMN {name} {decl}")
```

Wired into `make_engine` (the single engine factory) so every command path picks it up. Result: upgrades from 0.2.x are transparent — no `init` re-run, no DB wipe, no README guidance needed. Fresh installs hit this with `snapshot_items` absent → no-op until `init_db` creates the table with the columns already in `Base.metadata`.

The existing `init_db` docstring still talks about the 0.1→0.2 wipe policy and "Alembic in 0.3+"; updating that docstring is part of 0.3 implementation.

### Persistence

`create_snapshot(session, source, items: Iterable[dict[str, Any]])` ([snapshot.py:186](src/likesurgeon/snapshot.py#L186)) feeds raw provider dicts through a per-source translator into `SnapshotItem` rows. There's no post-translation hook to "attach" extra columns, so 0.3 augments the raw provider dicts *before* `create_snapshot` consumes them, rather than adding a new `per_item_metadata` parameter:

1. `scan youtube-likes` fetches the playlist items (raw dicts) and keeps each item's `playlistItems.list` snippet title within reach for stage 2.
2. Same command calls `fetch_video_statuses(...)` for the corresponding video IDs. This produces stage-1 `VideoStatus` objects (including the `"missing_from_videos_list"` placeholder for IDs absent from the response).
3. **Stage-2 disambiguation (still in the scan command, not the translator)** — for each `VideoStatus` whose `reason == "missing_from_videos_list"`, replace it with the final `VideoStatus` per the snippet-title rules in the Status mapping pipeline above. After this step, no `VideoStatus` carries the placeholder reason.
4. For each raw item dict, set a namespaced augmentation key — `item["_likesurgeon_video_status"] = {"is_available": <bool|None>, "reason": <str|None>}`. **Inject as a primitive dict, NOT the `VideoStatus` dataclass**, because the existing snapshot ingestion serialises `rec["raw"]` via `json.dumps(...)` ([snapshot.py:213](src/likesurgeon/snapshot.py#L213)) and dataclasses aren't JSON-native — a stray dataclass would crash the snapshot transaction. The underscore prefix marks the key as our internal injection, not a YouTube API field, so it can't collide with anything provider-side.
5. Pass the augmented dicts to `create_snapshot(...)` unchanged in shape (still `Iterable[dict[str, Any]]`).
6. The YouTube translator reads `item.get("_likesurgeon_video_status")` and writes the values onto its returned record dict — `rec["is_available"]` and `rec["unavailable_reason"]`. (Translators don't construct `SnapshotItem` rows themselves; they return record dicts which `create_snapshot()` then materialises into ORM rows by copying `rec[<field>]` into `SnapshotItem(<field>=…)` per [snapshot.py:196](src/likesurgeon/snapshot.py#L196).) The translator also **excludes the `_likesurgeon_*` namespace from `rec["raw"]`** (e.g. by building `rec["raw"]` from a filtered shallow copy of `item`) so `raw_json` continues to capture only genuine YouTube API data. It does NOT re-derive or re-disambiguate — by contract the `VideoStatus` value handed in is final. The YT Music translator ignores the field, omitting `is_available` / `unavailable_reason` from its rec (the columns stay NULL for `ytmusic_liked_songs` rows, consistent with the YT-only ghost-detection scope).
7. `create_snapshot()` is also extended to read `rec.get("is_available")` / `rec.get("unavailable_reason")` and pass them to the `SnapshotItem(...)` constructor alongside the existing fields. Missing keys default to `None`, preserving backward compatibility with translators that don't set them.

This keeps the existing translator pattern intact — no new `create_snapshot` parameter, no new shape contract beyond one private key on the raw dict. The whole snapshot still lands in a single transaction; partial commits are not introduced.

---

## Component 2 — Drift path (compare-time)

### Algorithm

New module `src/likesurgeon/drift.py`. Note that `SnapshotItem` has no `channel` column — both `youtube_liked_videos` and `ytmusic_liked_songs` rows store the relevant attribution as a JSON-encoded `artists: list[str]`. The drift signal is therefore "title changed meaningfully OR artists list changed" (which on the YouTube side maps to a channel rename / takedown-and-reupload, and on the YT Music side maps to artist re-tagging or `- Topic` migration):

```python
@dataclass(frozen=True)
class DriftFinding:
    source: str                          # "ytmusic_liked_songs" | "youtube_liked_videos"
    video_id: str
    prev_snapshot_item_id: int           # SnapshotItem.id, NOT Track.id
    curr_snapshot_item_id: int
    prev_title: str
    curr_title: str
    prev_artists: tuple[str, ...]        # decoded from artists JSON
    curr_artists: tuple[str, ...]
    title_similarity: float              # 0.0–1.0
    artists_changed: bool


def detect_drift(
    prev: list[SnapshotItem], curr: list[SnapshotItem], *, source: str
) -> list[DriftFinding]: ...
```

`SnapshotItem.id` (not `Track.id`) is the right pointer here because `Track` rows are upserted by canonical identity and overwritten with the latest metadata on each scan — so a `Track` reference would lose the point-in-time prev/curr distinction the moment the next scan runs. Snapshot items are immutable per scan, so they're the durable handle for "what we saw then" vs "what we see now".

Pseudocode:

```
prev_by_id = {p.video_id: p for p in prev if p.video_id}
findings = []
for c in curr:
    if not c.video_id: continue
    p = prev_by_id.get(c.video_id)
    if p is None: continue
    title_sim = rapidfuzz.fuzz.token_sort_ratio(p.title, c.title) / 100.0
    prev_artists = tuple(_decode_artists(p.artists))
    curr_artists = tuple(_decode_artists(c.artists))
    artists_changed = _normalize_artists(prev_artists) != _normalize_artists(curr_artists)
    if title_sim < 0.90 or artists_changed:
        findings.append(DriftFinding(source=source, ...))
return findings
```

`_decode_artists` deserializes the JSON list. `_normalize_artists` lowercases and strips each element (and probably reuses whatever the existing classifier / matcher uses for artist comparison — verify during implementation).

### Threshold rationale

- `title_similarity < 0.90` catches retitles, edition tags, language switches, and remaster suffixes while tolerating cosmetic punctuation/whitespace.
- `artists_changed` (any change after normalization) is a low-volume, high-signal event — channel rebrands and takedown-and-reupload on YouTube, artist re-tags or `- Topic` migration on YT Music. No threshold tuning.

Both signals are OR'd. v1 ships with the constant 0.90; tuning happens via real data, not via CLI flag.

### compare-likes integration

`compare-likes` currently selects each source's latest snapshot. 0.3 also needs the **previous** (penultimate) snapshot per source. The existing `list_snapshots(session)` helper takes no arguments and returns everything ordered by recency, so 0.3 introduces a small typed helper alongside it (signature subject to plan, but along these lines):

```python
def latest_snapshots_for_source(
    session: Session, source: str, *, limit: int = 2
) -> list[Snapshot]: ...
```

Drift detection then becomes:

```
for source in (YT_MUSIC, YOUTUBE):
    snapshots = latest_snapshots_for_source(session, source, limit=2)
    if len(snapshots) >= 2:
        findings = detect_drift(snapshots[1].items, snapshots[0].items, source=source)
        for f in findings:
            persist DiagnosisItem(issue_type='metadata_drift', ...)
```

If a source has only one snapshot (or none), drift detection is silently skipped for that source. No error, no warning row.

### Scope (drift)

Drift runs on **both YouTube and YT Music** sources. Unlike ghost detection (which has YT Music false-positive concerns from the external `videos.list` API), drift compares only our own snapshots. YT Music drift is real and frequent (artist channel renames, `- Topic` re-tags, `(feat. X)` edits) and costs nothing to detect.

---

## Component 3 — Glue (diagnosis / doctor / issues / CLI)

### New `issue_type` strings

Added to `src/likesurgeon/diagnosis.py`:

```python
ISSUE_UNAVAILABLE_VIDEO = "unavailable_video"
ISSUE_METADATA_DRIFT = "metadata_drift"
```

### DiagnosisItem persistence

Inside the `compare-likes` flow, in addition to the current three findings buckets:

- For each YouTube SnapshotItem with `is_available=False`: emit `DiagnosisItem(issue_type=ISSUE_UNAVAILABLE_VIDEO, source_track_id=item.track_id, related_track_id=None, confidence=1.0, reason=f"video unavailable: {item.unavailable_reason}")` (e.g. `"video unavailable: deleted"`). The short `unavailable_reason` token stays terse for filtering; the `reason` text field carries the human-readable form.
- For each `DriftFinding`: emit `DiagnosisItem(issue_type=ISSUE_METADATA_DRIFT, source_track_id=curr_snapshot_item.track_id, related_track_id=None, confidence=1.0, reason=...)`. The `confidence` for drift is **not** `title_similarity` — `--min-confidence 0.7` would otherwise hide the *worst* retitles (lower similarity = bigger drift). Drift findings are deterministic post-filter, so confidence is simply `1.0` and the actual severity (similarity score, before/after values, snapshot-item ids) is encoded in the `reason` text:
  ```
  reason = (
      f"source={finding.source}; "
      f"prev_item={finding.prev_snapshot_item_id}, curr_item={finding.curr_snapshot_item_id}; "
      f"title: {finding.prev_title!r} → {finding.curr_title!r} (sim {finding.title_similarity:.2f}); "
      f"artists: {list(finding.prev_artists)} → {list(finding.curr_artists)}"
  )
  ```
  We deliberately store `source_track_id=curr.track_id` (the still-meaningful "this is the currently-affected track") with `related_track_id=None`, since the `Track` row gets upserted to the latest metadata on every scan and the prev/curr `Track` references would usually be the same row anyway.

  The `SnapshotItem` ids in `reason` are **diagnostic only — not a parseable contract**. They're embedded so a human reading `issues --type metadata_drift` output can correlate the finding back to specific snapshot items, not so future code can string-parse them. If later tooling needs structured access to prev/curr snapshot items, the right move is to extend `DiagnosisItem` with proper FK columns (`source_snapshot_item_id`, `related_snapshot_item_id`) at that point — don't depend on the text format. The format is allowed to drift between releases.

### Doctor

`HealthSummary` adds two count fields populated from `DiagnosisItem.issue_type` aggregation:

```python
unavailable_videos: int
metadata_drift: int
```

`doctor` CLI output gets two new rows under the existing diagnosis-summary section. The "match-rate health score" formula is **not** modified.

### Issues filter

`issues --type <name>` is already string-routed, so the *filter logic* needs no change — the two new types pass through automatically:

```bash
uv run likesurgeon issues --type unavailable_video
uv run likesurgeon issues --type metadata_drift
```

But the command's `--type` help text in [cli.py](src/likesurgeon/cli.py) currently lists only the three pre-existing types (`possibly_missing_from_ytmusic | possible_pointer_drift | ytmusic_only`). 0.3 updates that help string to enumerate all five. README's `### 5. Compare across sources` section also adds an example for one of the new types.

### CLI surface

**No new commands.** Behavioural changes only:

- `scan youtube-likes` is ~1 second slower per 5K likes (extra `videos.list` round-trip).
- `compare-likes` reports two more finding types in its summary table.
- `doctor` prints two extra rows.
- `issues --type ...` accepts the two new strings.

---

## Error handling

- **`videos.list` quota exhaustion / network failure** — logged warning, batch items persisted with `is_available=None, unavailable_reason="status_check_failed"`. Scan completes.
- **Single ID missing from response** — treated as `is_available=False`; reason is disambiguated via the captured `playlistItems.list` snippet title per the Status mapping table (`"private"` / `"deleted"` / `"unavailable"`).
- **No previous snapshot for a source** — drift detection silently skipped for that source.
- **Both snapshots present but neither has the same `video_id` for any items** — drift detection runs and produces zero findings; no error.
- **Schema migration** — silent additive `ALTER TABLE` at engine-construction time (see the "Schema migration" subsection above). No DB wipe, no `init` re-run. The README "Upgrading from 0.1" section is removed entirely; no replacement section is added for 0.3 since the migration is transparent.

---

## Testing approach

Unit tests:
- Status mapping (`uploadStatus=rejected` → `reason=rejected`, etc.) including the snippet-title disambiguation for missing-from-response IDs (`"Private video"` → `"private"`, `"Deleted video"` → `"deleted"`, anything else → `"unavailable"`).
- `fetch_video_statuses` batches into 50-ID chunks; per-batch retry on 5xx; quota-exceeded short-circuits remaining batches.
- `detect_drift` pure function: title-fuzzy threshold, artists-changed-only path (channel rename / `- Topic` migration), no-prev / no-curr / unmatched-id paths.
- Reason-string formatting in DiagnosisItem persistence (drift reason includes `prev_item=…, curr_item=…, source=…`).

Integration tests (using existing in-memory SQLite fixture):
- `scan youtube-likes` with a stubbed `YouTubeClient` persists `is_available` columns correctly, AND the resulting `SnapshotItem.raw_json` does NOT contain the `_likesurgeon_video_status` injection (proves the translator strips the private namespace from `rec["raw"]` and that `json.dumps(rec["raw"])` doesn't choke on a non-serialisable dataclass).
- `compare-likes` against a fixture with one ghost item + one drift candidate produces both DiagnosisItem rows in addition to the existing buckets.
- `doctor` count aggregation includes the two new types.
- `issues --type unavailable_video` filters to ghost rows.

CLI smoke (manual, not automated):
- `scan youtube-likes` against a real account — verify scan completes, status quota visible in Google Cloud Console.
- `compare-likes` after a deliberate test drift (manually edit a SnapshotItem to mimic a title change) — verify drift finding appears in `issues`.

---

## Implementation order (sketch — feeds into writing-plans)

1. Schema columns + model changes (`SnapshotItem.is_available`, `unavailable_reason`) + `_migrate_in_place` helper wired into `make_engine` + `init_db` docstring update. Tests for the migration: fresh install no-op, 0.2-shape DB gets columns added, double-call is idempotent.
2. `YouTubeClient.fetch_video_statuses` + `VideoStatus` dataclass + status mapping + batch retry/quota-exceeded policy + tests.
3. `scan youtube-likes` integration: stage-1 `fetch_video_statuses` call, stage-2 snippet-title disambiguation, inject primitive-dict augmentation onto raw items, YouTube translator reads it into `rec["is_available"]` / `rec["unavailable_reason"]` and strips the `_likesurgeon_*` namespace from `rec["raw"]`, `create_snapshot()` extended to copy those rec fields onto `SnapshotItem(...)`. Tests including the raw_json-cleanliness assertion.
4. `drift.py` module: `DriftFinding` (with `source`, `prev_snapshot_item_id`, `curr_snapshot_item_id`, artists list pair) + `detect_drift` + tests.
5. New helper `latest_snapshots_for_source(session, source, *, limit=2)` in `snapshot.py` (or a sibling module if `snapshot.py` is getting busy) + tests.
6. `compare-likes` integration: pull prev snapshot via the new helper, run ghost finder + drift finder, persist new DiagnosisItem types. Tests.
7. `doctor` count aggregation + `issues --type` help-text update (enumerate all five types) + tests.
8. README updates: feature line under "What it does today", roadmap row update, `### 5. Compare across sources` example for one new type, delete the "Upgrading from 0.1" section.

The detailed plan is the responsibility of writing-plans; this design is the input to that step.
