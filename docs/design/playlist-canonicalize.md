# Playlist canonicalize

Spec for `canonicalize-playlists`. The report finds playable audio tracks in your own playlists whose artist page lists another release of the same song. `--apply` replaces each one with that release on YouTube (repoint). When the playlist already holds that release, it removes the extra entry (merge). The code:

- `playlist_canonicalize.py`: discovery, report and planner.
- `playlist_dispatch.py`: the writes, shared with `repoint-playlists`.
- `cli.py`: `canonicalize_playlists_cmd` and the shared `_playlist_apply`.

This spec gives the model and the rules they follow. It builds on [canonicalize.md](canonicalize.md) (the likes version) and [playlist-repoint.md](playlist-repoint.md) (the playlist model and the dispatcher's repoint flow).

## Model

This is a preference, not a repair, as with likes canonicalize. A playlist item A is playable and YT Music shows it as itself. Its artist page lists another release B of the same song (the album track rather than the single, say). Moving A to B makes the playlist hold the release the artist page lists.

A playlist is one store that YT Music renders, as in playlist-repoint. The YouTube side (`playlistItems.list`) is the store, and the YT Music side shows it in order. A must be shown **as itself, for a playable YouTube item of its own**: that item is what gets deleted. An A that YT Music shows for another item (a dead video, or an MV shown as its audio) belongs to `repoint-playlists` or to nobody, never to this command. So no item is a finding of both commands.

## Discovery

It reads the latest stored playlist scan, so the report costs no YouTube quota.

1. **Eligible playlists**: your own playlists (`playlist_owned` true) with a YouTube side. Saved playlists, playlists whose ownership is unknown and playlists without a YouTube side are ignored.
2. **Artist ids**: the distinct `artists[].id` of the **audio tracks** (`MUSIC_VIDEO_TYPE_ATV`) on the YT Music side of every eligible playlist, in first-seen order. Only an audio track can be an A. Each artist page is read once (`YTMusicClient.fetch_artist_songs`), however many playlists credit that artist. Before the reads, a one-song Liked songs read probes YT Music auth, so stale browser cookies are refreshed once before the slow part.
3. **Per eligible playlist**: `canonicalize.find_candidates(that playlist's raw YT Music tracks, artists)`, unchanged. This is the likes rule: A is an ATV that none of its artists' lists (Show all or Top songs) contains, while exactly one ATV B in the Show-all lists has an equal title key and a length within 1 s. An unreadable artist blocks its A's. A B credited to other artist ids than A is still a finding, report-only (below).
4. **Own playable item**: `align.align(YouTube side, YT Music side)` for the playlist. A candidate is kept only if some YT Music entry showing A is backed by a YouTube item with the **same** video id whose availability is known to be playable (`is_available is True`). The first such backing gives A's YouTube row and A's YT Music row.

The report counts, summed over playlists (a song in two playlists counts twice):

- A's already listed by an artist
- A's with an unreadable artist
- A's with two or more B's (ambiguous: counted, no finding)
- single-B candidates not shown as their own playable item (dead, availability unknown, or YT Music's rendering of another item)

The first real report read 189 artist pages (4 unreadable) in about 4.5 minutes and saved 16 findings, 15 of them write-eligible.

## The finding

`playlist_canonical_version`, in a `Diagnosis` with `kind = 'playlist_canonicalize'` and `playlist_scan_id` = the scan it was built on. Per finding:

- `source_item_id` = A's `youtube_playlist` row; `source_track_id` = that row's track.
- `related_item_id` = A's `ytmusic_playlist` row: the entry rendering A's item, as for `playlist_relinked`, where that entry is B.
- `related_track_id` = B's `Track`, created from the artist-page song (`snapshot.upsert_ytmusic_track`).

The reason names the playlist, A's 1-based position, A and B (`canonicalize.describe`: id, title, album, length), the artist whose list holds B and whether B is already in the playlist (on its YouTube side). For example: `playlist 'Road Trip', position 2; A=vidA 'Paper Lantern' (album 'Paper Lantern - Single', 200 s); B=vidB 'Paper Lantern' (album 'Night Market', 200 s); artist 'Invented Band' lists B; B already in the playlist: no`.

Confidence 1.0 is write-eligible. 0.5 is report-only, and the reason ends with every cause that applies (`report_only_cause`):

- a version keyword in A's album (`Version`, `Ver.`, `Live`, `Remix`, ...; the likes list), read from A's YT Music row. Only this cause can be lifted by `--include-flagged`.
- different performers: A's and B's sets of YT Music artist ids differ, or B has none (`canonicalize.performers_cause`, canonicalize.md §2). The reason names both credit lists. Ids, not names, so one artist under two spellings still matches.
- A listed more than once on the YouTube side, or shown more than once by YT Music.
- B not either absent from both sides or listed once and shown once. Examples: B shown only as the rendering of a dead item, B listed but hidden, B twice.
- B's item in the playlist isn't playable (listed once and shown once, but its availability is dead or unknown, or YT Music marks its track `isAvailable=False` in the scan). A merge would delete the playable A and keep a B that may not play. `--apply` re-checks this live: a merge whose YT Music baseline marks B unavailable gets no writes (a failed `playlist_precheck` row, the item stays open, the playlist isn't stale), and a repoint whose inserted B YT Music shows unavailable fails `playlist_ytm_verify` like a B it doesn't show (B removed, A kept, item skipped, run stops). A is never deleted for an unavailable B.

All causes except the keyword and the performers come from the playlist's shape (`structural_cause`). The planner recomputes every cause and never parses `reason`: the keyword and A's artist ids from A's YT Music row in the report's scan, B's artist ids from B's `Track` (the artist-page song dict the report stored), the rest from the scan.

Skips carry over as for `playlist_relinked`: by (issue type, A's track, B's track, A's playlist). So a skip covers the same pair in the same playlist only, and never carries to or from a `playlist_relinked` finding.

## Planner

`playlist_canonicalize.plan(session, diagnosis, *, include_flagged)` turns open findings at 1.0 into actions. With `--include-flagged` it also takes an open 0.5 finding whose only cause is the keyword (keyword present, `performers_cause` and `structural_cause` None).

- **Order**: smallest playlist (YouTube side count), then playlist id, then A's position, as `repoint-playlists`. So `--limit 1` is the smallest first write.
- **Action**: `a_index` and `a_playlist_item_id` from A's YouTube row (its raw `snippet.position` and `id`). `b_index` = A's YT Music row position: the index of the entry rendering A's item, which for a canonical action is A itself.
- **Forecast kind**: `canonical_merge` when B is on the playlist's YouTube side in the scan, or an earlier action of this plan leaves B there (two A's sharing one B: repoint, then merge). Otherwise `canonical_repoint`. The kind is a forecast for the plan table and the quota estimate only. The dispatcher picks the flow from the live baseline.
- **States**: `playlist_repoint.playlist_states`, the same builder `repoint-playlists` uses.

## Action flows

For A at YouTube index *p* (item id *a*), shown by YT Music as A at index *j*, with target B, in playlist P. The baseline step is the existing one ([playlist-repoint.md](playlist-repoint.md), step 0): live reads equal to the scan plus this run's applied actions. Each applied action's removed and landed indices move later actions' *p* and *j*. Nothing lands in a merge, so a later index past the removed slot moves up one.

**Canonical precheck** (row `playlist_precheck`). The YouTube side holds A's item at *p*, YT Music index *j* shows **A** (not B), and B is on both sides of the baseline or on neither. Anything else means no writes in P this run: its items stay `open` and other playlists continue.

The baseline then picks the flow: B on the YouTube side means merge, otherwise repoint.

### Repoint (B on neither side)

After the precheck this is the existing `repoint` flow, and its `_Change` expressions already describe it. The insert goes in before A's delete, B is removed only while A is known to be in place, A is put back before B is removed after a delete, and sorted playlists are handled as in playlist-repoint.

| Check | Expected without *x* | *x* | Positional `at` |
|---|---|---|---|
| `playlist_insert_verify` | YouTube baseline | B's new item | *p* |
| `playlist_ytm_verify` | YT Music baseline | B | *j* (A moves to *j* + 1) |
| `playlist_delete_verify` | YouTube baseline without A's item | B's item | *p* |
| `playlist_check` | YT Music baseline without entry *j* | B | *j* (B shows where A was) |

In an automatically sorted playlist `at` is None, so B lands where the sort order puts it.

### Merge (B on both sides)

A merge only deletes. Nothing moves in, so both checks are exact lists, in positional and sorted playlists alike. **B is never touched: it is the entry being kept.**

1. `playlist_delete` of item *a*, inside the interrupt guard. If the quota rejects it, the action is `failed` with nothing sent.
2. Wait 5 s. `playlist_delete_verify`: the YouTube side equals the baseline without item *a*, exactly (one 15 s re-read). If item *a* is still listed, or the side is unreadable, nothing is undone: the pair goes to `delete_unconfirmed` and the run stops.
3. `playlist_check`: YT Music equals the baseline without entry *j*, exactly (one 15 s re-read).
4. If item *a* is gone but the YouTube side is otherwise off, or YT Music is off or unreadable, A is restored. `playlist_restore` inserts A at *p*. In a sorted playlist it goes without a position, switching on a refused position as usual. Then `playlist_restore_verify` checks for one new item of A (an item id not in the baseline), at *p* when positional, with the rest exactly the baseline without item *a*. Confirmed: `skipped`, counted `restored`, and the run stops. Not confirmed: A goes in `left_removed` (B stays) and the run stops.
5. Both checks pass: the action is `applied`. It is `failed` and the item stays `open` when the delete call errored but landed; the run goes on. The post-check sides become P's next baseline. A merge is never counted in `reordered`.

A duplicate A (report-only, but reachable by hand) deletes only its own item id. A restore counts only an item id that isn't in the baseline, so the other copy is never taken for A put back.

### Outcomes

| Case | Item status | Run |
|---|---|---|
| Repoint or merge, every call succeeded, both checks pass | `applied` | continues |
| Delete errored but landed, both checks pass | `open` (counted `failed`) | continues |
| Precheck fails (A not at *p*, *j* doesn't show A, B on one side only, or the playlist changed) | `open` | continues; that playlist gets no writes |
| Quota rejected the merge's delete (nothing sent) | `open` | stops (quota) |
| Merge delete not confirmed | `open`, `delete_unconfirmed` | stops |
| Merge undone: A put back | `skipped`, counted `restored` | stops |
| A couldn't be put back after a merge | `open`, `left_removed` | stops |
| Repoint failures | as in playlist-repoint | as in playlist-repoint |

## Row kinds and what settles

The rows are the existing `playlist_*` kinds. A merge writes:

- `playlist_precheck`, only when the baseline is read (not when the playlist's previous action just confirmed it)
- `playlist_delete`, `playlist_delete_verify`, `playlist_check`
- to put A back: `playlist_restore` (two rows when a sorted playlist refused the position) and `playlist_restore_verify`
- `playlist_interrupted` when anything stops the action once the delete call started

Only an applied `playlist_check` or `playlist_rollback_verify` settles an action. A clean merge settles with its `playlist_check`. **A merge whose A was put back has no settling row** (Decision 7), so `unsettled_playlist_actions` keeps listing it until both sides of its playlist are scanned again, which the restore's stop calls for anyway. The unsettled list reads B from the finding's related `Track` for both commands, since a canonical finding's related item is A itself.

If the `playlist_interrupted` row itself can't be committed, a stderr line names the playlist, A and B. It says the delete may still land, to change nothing yet and leave B as it is, to re-scan, and to add A back only if it is missing. It never advises removing B: in a merge B is the entry being kept, and removing it while A's delete is pending could lose both.

## Operating rules

- **Report on the latest scan.** `canonicalize-playlists` exits 1 without a playlist scan. `--apply` exits 1 when a newer playlist scan exists than the report's: re-run `scan playlists` and `canonicalize-playlists`.
- **Per-playlist live check.** Each playlist's first action reads both sides and compares them with the scan. A playlist either playlist command wrote to since the scan fails this and gets no writes. There is no run-wide refusal after earlier writes, and playlist rows never count toward `sync`'s refusal.
- **Order and slices.** Smallest playlist first. `--limit N` caps findings per run and `--yes` skips the prompt.
- **`--include-flagged`** adds keyword-only findings. The plan table then shows a Keyword column.
- **Plan table.** ID, playlist, A's position, forecast kind, A, B, and B's title (from the finding's related `Track`; `repoint-playlists` keeps reading B's title from its scan row, which is B there). The result line, the stale-playlist list, the unsettled list and the left-behind lines are shared with `repoint-playlists` (`cli._playlist_apply`), worded so they hold for a merge too.
- **Quota.** A repoint is an insert and a delete (50 units each, about 100) plus a read of the YouTube side after each. A merge is one delete (50) plus one read. Each playlist is read once as its baseline (1 unit per page of 50 items). Artist pages and every YT Music read are free. Re-reads, undo calls (about 50 each) and the one refused positional insert per automatically sorted playlist aren't estimated. The plan mentions the refused insert only when it has a repoint. Running out stops the run after the action in flight settles.

## Decisions

1. **Command**: a new `canonicalize-playlists` with `--apply [--limit N] [--yes] [--include-flagged]`, shaped like `repoint-playlists`. Alternative: `canonicalize --playlists`. Rejected because `canonicalize` reads Liked songs live and has `--json`, while this command reads a stored scan and needs the stale-scan refusal.
2. **Storage**: `Diagnosis.kind = 'playlist_canonicalize'`, one issue type `playlist_canonical_version` (1.0 write-eligible, 0.5 report-only), item ids as above, no schema change. A's with two or more B's are only counted. Alternative: a second report-only issue type, as likes has `canonical_ambiguous`.
3. **Input**: the latest stored playlist scan, so the report costs no YouTube quota. Alternative: read every playlist again inside the command.
4. **Repoint or merge**: decided at run time from the action's baseline. The planned kind is a forecast, as in likes canonicalize. Alternative: fix the flow at plan time and treat a differing baseline as stale.
5. **Report-only causes**: the version keyword (the only one `--include-flagged` lifts), different performers, A more than once, B not cleanly absent or present once, and B's item not playable. All are recomputed from the scan at plan time. Alternative for duplicates: act on each copy by item id.
6. **Duplicates covered**: two releases of one recording in a playlist where the artist page lists exactly one of them (a merge keeps that one). Pairs the artist page lists both or neither of, and the same video twice, get no action. Alternative: keep the earlier-positioned one.
7. **A merge whose A was put back has no settling row**, so it stays unsettled until its playlist is re-scanned. Alternative: let an applied `playlist_restore_verify` settle it. Rejected because it would also settle a repoint whose B removal was quota-rejected.

## Known limits

- **A wrong B that renders as itself passes every check.** YT Music shows B as B, so the checks can't tell a right pair from a wrong one. The candidate rule (same artist list, ATV, equal title key, ±1 s, same artist ids), the keyword net and the review before `--apply` are the guards. Two recordings by the same credited artists with the same title and length still pass (canonicalize.md §7). `skip` anything doubtful.
- **Sorted playlists move B.** In an automatically sorted playlist a repoint's B goes to its sort place, not A's index. A merge removes A and leaves B where it is.
- **About 150 songs per Show-all list.** For a large catalog, A may sit past the cap (a finding although listed further down) or B may be missed (canonicalize.md §7).
- **Duplicates not covered.** Two releases the artist page lists both or neither of, and the same video listed twice, stay as they are.
- **Stale reads.** A YT Music read served from before a write would pass a check, as in playlist-repoint.

## First live run (2026-11-16)

- **Report:** 189 artist pages read (4 unreadable, YT Music omitted their songs list), 16 findings, 15 write-eligible. The one report-only finding had a version keyword in A's album.
- **One action first:** a repoint in a 10-item manually sorted playlist. All seven rows were `applied`. After a re-scan the finding was gone, and the playlist still held 10 items with B in A's slot.
- **The rest:** 14 actions in 6 playlists, 12 repoints and 2 merges. All applied, with no failure, skip or restore. 11 of the repoints were in playlists sorted by date added (newest first), so their B went to the top. The 12th was in a manually sorted playlist and kept A's position. Each merge removed exactly one entry, and the two playlists it touched shrank by one.
- **After the run:** a re-scan and report showed 0 write-eligible findings (only the report-only one remained). Audio tracks already listed by their artist went from 456 to 469.
- **Quota:** the plan estimated 1,375 units for the 14. The report itself used none.
