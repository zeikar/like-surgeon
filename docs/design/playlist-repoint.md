# Playlist repoint

Spec for `repoint-playlists`: a report of dead items in your own playlists that YT Music shows as another video (A → B), and `repoint-playlists --apply`, which replaces A's playlist item with B through the YouTube Data API: at the same position in a manually sorted playlist, and where the sort order puts it in an automatically sorted one. The code is in `playlist_repoint.py` (pure pairing, report, planner) and `playlist_dispatch.py` (the writes); this is the model and the rules they follow.

## Model

A playlist is one store that YT Music renders, as LL → LM is for likes: the **YouTube side** (`playlistItems.list`, each item has an item id and a video id) is the store, and the **YT Music side** (`get_playlist`) shows the same entries in the same order, with a playable version (B) in place of a region-blocked item (A). Deleted and private items aren't shown at all. So the pairing is `align.align(youtube side, ytmusic side)` run per playlist, on the stored scan: anchors are entries whose video id is on both sides, and the gaps between them are paired in order when their sizes are equal. Playlists are independent; nothing crosses from one to another.

A repoint is not a new item replacing an old one in the store's eyes: A's item is removed and B's item put at the same index, so the YouTube side changes and the YT Music side must read exactly as before. That holds for a **positional** (manually sorted) playlist. An automatically sorted playlist (**sorted**) decides where every item goes, so A's item still leaves A's index but B's item lands where the sort order puts it, and YT Music shows B there instead of in A's slot (see [Sorted playlists](#sorted-playlists)).

## The finding

`playlist_relinked`: a dead A on the YouTube side that alignment pairs with a B whose video id differs. Stored in a `Diagnosis` with `kind = 'playlists'` (`playlist_scan_id` = the scan it was built on), one `DiagnosisItem` per pair, with `source_item_id` = A's `youtube_playlist` row and `related_item_id` = B's `ytmusic_playlist` row (`source_track_id` / `related_track_id` as for likes).

Confidence 1.0 is write-eligible; 0.5 is report-only, and the reason says which cause (`report_only_cause`, first match wins):

- ownership of the playlist unknown, or a saved (not owned) playlist (such a playlist has no YouTube side in new scans, so this only occurs on older ones)
- B is also on the playlist's YouTube side (YT Music shows B twice)
- YT Music shows B other than once
- no `videos.list` metadata for A, or for B
- the same-recording check fails (duration within ±3 s and same channel or overlapping titles)

Dead items that never become findings are counted in the report: ones YT Music shows as themselves, and ones it doesn't place (deleted and private items sit in unequal gaps, as does any gap alignment can't pair). Real-account numbers from the first run: 35 playlists, 199 dead items, 45 findings of which 43 are write-eligible (2 have B already on the YouTube side), 28 dead items shown as themselves, 126 not placed. All 45 had a region-blocked A.

## Action flow

For A at YouTube index *p* (item id *a*), shown by YT Music as B at index *j*, in playlist P. Every write is committed to `sync_attempts` at once, before any wait. The module docstring of `playlist_dispatch.py` is the authority; this is its summary.

**One check form.** Every check except the one after removing B has the same shape (`_only_change(got, expected without x, x, at)`): the read holds one moving element *x*, and with that occurrence removed it is exactly the expected list, order included. Nothing else may move, appear or disappear. In a positional playlist *x* must also sit at index `at`; in a sorted playlist it may sit anywhere. The YouTube side is compared by `(item id, video id)`, YT Music by video id.

| Check | Expected without *x* | *x* | Positional `at` |
|---|---|---|---|
| `playlist_insert_verify` | YouTube baseline | B's new item | *p* |
| `playlist_ytm_verify` | YT Music baseline | one more B | *j* (B already shows there, so either side of it reads the same) |
| `playlist_delete_verify` | YouTube baseline without A's item | B's item | *p* |
| `playlist_check` | YT Music baseline without index *j* (A's rendering) | B | *j* (the list reads exactly as before) |
| `playlist_restore_verify` | nested: with A's new item removed, the rest must be the YouTube baseline without A's old item plus B's item (B at *p* when positional) | A's new item: the one video-A item whose item id isn't in the baseline | *p* (so B sits at *p* + 1) |
| identifying an errored insert | YouTube baseline | the one new item, video B | *p* for the run to go on |
| `playlist_rollback_verify` | exactly the baseline (or, after a restore, the restore read without B's item) | none | none |

0. **Baseline**: the previous action's matching post-check reads of P, else fresh reads of both sides that must equal the scan plus this run's applied repoints, with A and B where the action says (row `playlist_precheck`). A difference means no writes in P this run: its items stay `open` and other playlists continue. An unreadable side stops the run. *p* and *j* are first moved by each applied repoint in P this run: its removed and landed index on each side, which are equal in a positional playlist, so nothing moves there.
1. **Insert B** (`playlist_insert`): at *p* in a positional playlist, without a position in a sorted one. If a positional insert gets `manualSortRequired`, see [Sorted playlists](#sorted-playlists). Wait 5 s. Check the YouTube side (`playlist_insert_verify`), then YT Music (`playlist_ytm_verify`). Each check re-reads once after 15 s on a mismatch.
2. **Delete item *a*** (`playlist_delete`; anything stopping the action from here on leaves a `playlist_interrupted` row). Wait 5 s. Check the YouTube side (`playlist_delete_verify`; item *a* must be gone), then YT Music (`playlist_check`). If both match, the action is `applied` and these reads become P's next baseline.
3. **Undo**. B is removed (`playlist_rollback`, `playlist_rollback_verify`) only while A's item is known to be in place: before the delete call, when the quota rejected the delete, or after A was put back. After a delete that was sent:
   - The YouTube side can't confirm item *a* gone (still listed after the re-read, or unreadable): nothing is undone. The delete may still land, so B stays and the pair is reported (`delete_unconfirmed`).
   - Item *a* is gone but either side doesn't match, or YT Music can't be read: A is inserted again (`playlist_restore`, at *p* or, in a sorted playlist, without a position; `playlist_restore_verify` as in the table). Only once that is confirmed is B removed. If A can't be put back, B stays and A is reported (`left_removed`).
   - A B that can't be removed or confirmed gone is reported (`left_inserted`).

A positional repoint keeps the playlist's length and every other slot. A sorted one keeps the length and the order of every other item. Either way the next action's indices are known: unchanged, or moved as in step 0.

| Case | Item status | Run |
|---|---|---|
| Every call succeeded, both checks pass | `applied` | continues |
| Delete errored but landed, both checks pass | `open` (counted `failed`) | continues |
| YT Music didn't show the inserted B; B removed again | `skipped` | stops |
| A was put back after the delete (restored) | `skipped` | stops |
| Quota rejected the insert (nothing sent) | `open` | stops (quota) |
| Quota rejected the delete; B removed again | `open` | stops (quota) |
| Positional insert refused, playlist sorted automatically (`MANUAL_SORT_REJECTED`) | as the unpositioned retry ends | as the retry ends |
| Every call succeeded in a sorted playlist, both checks pass (B at its sort place) | `applied`, counted in `reordered` | continues |
| Insert errored and landed at A's index (anywhere in a sorted playlist); B removed | `open` | continues |
| Insert errored and landed elsewhere, or isn't listed after the re-read (it may still land) | `open`, B in `left_inserted` or removed | stops |
| 2xx insert not listed after the re-read | `open`, B in `left_inserted` | stops |
| YouTube side after the insert isn't the expected list; B removed | `open` | stops |
| Delete not confirmed | `open`, `delete_unconfirmed` | stops |
| A couldn't be put back | `open`, `left_removed` | stops |
| B couldn't be removed | `open`, `left_inserted` | stops |
| A side unreadable | `open` | stops |
| Live playlist differs from the scan | `open` | continues; that playlist only |

`skipped` is terminal (`unskip` re-opens it). Everything else stays `open`. The CLI exits 1 when any action failed, the run stopped early, the quota ran out or a playlist got no writes.

### Sorted playlists

A playlist sorted automatically (date added, popularity, date published, ...) refuses `playlistItems.insert` with `snippet.position`: HTTP 400, reason `manualSortRequired`, `Playlist should use manual sorting to support position.` The client raises `PlaylistManualSortRequired` for it (reason in the error body, else that message). It is not a "may still land" error: nothing was added. So:

- The refused insert's row is `failed` with a reason starting with `MANUAL_SORT_REJECTED`. Like `QUOTA_REJECTED`, it is not a sent write for `unsettled_playlist_actions`.
- P is `sorted` for the rest of the run, and the insert is sent again without a position (a second `playlist_insert` row). Later inserts in P, whether B's or A's in a restore, go without a position from the start.
- The checks take *x* anywhere (`at` = None), so B is accepted where the sort order puts it, while every other item must keep its order. A popularity order that shuffles other items between reads therefore fails a check, and the action is undone as usual.
- Since B isn't at *p*, the post-check reads, not "baseline with B at *p*", become the next baseline. The landed indices move later actions' *p* and *j* (step 0): with newest-first, B lands at the top, so a later A (after the removed one) moves up a slot for the removal and back down for B and keeps its index; with oldest-first, B lands at the end and a later A moves up one slot.
- Every rule above about when B may be removed and when A is deleted holds unchanged.
- `PlaylistExecResult.reordered` counts the repoints whose checks passed in sorted mode, and `sorted_playlists` names the playlists. The CLI prints both, since B is not where A was.
- Quota: one extra insert call (50 units) per sorted playlist, the refused one. The plan's estimate leaves it out and says so.

## Row kinds

All `SyncAttempt.kind` values start with `playlist_`.

- **Writes**: `playlist_insert` (two rows when a sorted playlist refused the position: the `MANUAL_SORT_REJECTED` one and the retry), `playlist_delete`, `playlist_rollback` (removes B), `playlist_restore` (puts A back), and `playlist_interrupted` (a marker, also on Ctrl-C, after the delete call started).
- **Reads and checks**: `playlist_precheck`, `playlist_insert_verify`, `playlist_ytm_verify`, `playlist_delete_verify`, `playlist_check`, `playlist_rollback_verify`, `playlist_restore_verify`.

Only an applied `playlist_check` (B in, A out, both sides confirmed) or an applied `playlist_rollback_verify` (B out, A in place) settles an action; every other row may leave the playlist changed or half-changed. `DiagnosisItem.reason` is never modified.

## Operating rules

- **The report is built on the latest scan.** `--apply` exits 1 when a newer playlist scan exists than the report's. Re-run `repoint-playlists` after `scan playlists`.
- **Per-playlist live check.** Each playlist's first action reads both sides and compares them with the scan; any difference means no writes in that playlist this run. Later actions reuse the previous action's post-check reads. There is no run-wide refusal after earlier playlist writes, so `--limit` slices don't force a re-scan between them; the report is stale after any run, though, and the next `--apply` needs a fresh scan and report.
- **Order.** Playlists with the fewest items first, then by playlist id and A's position, so `--limit 1` is the smallest possible first write.
- **`--limit N`** caps findings per run. **`--yes`** skips the prompt.
- **Quota.** Insert and delete cost 50 units each, `playlistItems.list` 1 per page of 50. The plan's estimate is, per playlist, one read of its YouTube side, and per action 2 writes plus a read with one more item and one with the original count (about 100 units plus the reads). Re-reads, undo calls (50 each), the refused positional insert of each automatically sorted playlist (50) and YT Music reads (free) aren't estimated. Running out stops the run after the action in flight settles; exit 1.
- **Unsettled actions.** `repoint-playlists --apply` lists, per playlist title, position, A and B, every action with a write row after its last settling row. An entry drops off once a YT Music and a YouTube scan of that playlist are both newer than its last write row. Playlist rows are not in `sync_preflight`'s `_WRITE_KINDS` or `_SETTLING_KINDS`: they never block `sync`, and they never count toward the stranded-likes list.
- **YT Music reads refresh stale cookies** once (`YTMusicClient.fetch_playlist`), as the likes checks do.

## Evidence

A live probe on 2026-11-15, on an owned 16-item playlist of an account that has no YouTube channel. The probe added two items and removed exactly those two. Nothing that was already in the playlist was moved or deleted, and both sides matched their pre-probe state afterwards.

- **Positional insert works.** `playlistItems.insert` with `snippet.position = 1` succeeded on the owned playlist even though the account has no channel. The new item sat at index 1, and every other item id kept its order.
- **YT Music reflects edits fast.** The inserted video showed in the YT Music playlist at the same index after about 0.6 s, and the rest of the YT Music list was unchanged. After the delete, the YT Music list was back to its original state after about 0.5 s.
- **The YouTube side can lag right after a delete.** One `playlistItems.list` read taken about a second after `playlistItems.delete` still differed from the original list. A read a few seconds later matched. Verification after a delete therefore needs a wait and a re-read before it treats a mismatch as real.
- **A region-blocked video can be inserted again.** The API accepted an insert of the playlist's own region-blocked video at index 1. YT Music showed, in that slot, the same playable video it already shows in place of that blocked video elsewhere in the playlist. The list grew by one and the rest was unchanged. This confirms the rendering model (YT Music shows a playable version in place of a blocked item), and that putting a region-blocked A back during an undo is possible.
- **Quota.** Taken from the documented costs, not measured: 50 units per insert or delete, 1 per `playlistItems.list` page.

### First live run (2026-11-15)

- **Manually sorted playlist (16 items):** 1 repoint, then the remaining 5. All 6 applied; each action's seven rows were `applied`. After a re-scan the playlist still held 16 items in YT Music, every song at its old position, and none of its dead items was left.
- **Automatically sorted playlists:** the first positional insert in one was refused (`HTTP 400: Playlist should use manual sorting to support position.`), which led to sorted mode. After it, 37 repoints in 8 automatically sorted playlists (all sorted by date added, newest first) applied with no failure, skip or restore. Each B went to the top of its playlist; the other songs kept their relative order and moved down by the number of repoints in that playlist. Every playlist kept its song set and count.
- **Totals:** 43 of the 45 findings applied; the other 2 (B already on the playlist) stay report-only. Dead items on the YouTube side went from 199 to 156.
- **Quota:** a day of repeated playlist scans (about 200+ units each) plus the probe and the first repoints exhausted the daily quota once; the run stopped before sending anything and continued cleanly after the reset.

The first real `--apply` (2026-11-15) worked on a manually sorted playlist. On an automatically sorted one, `playlistItems.insert` with `snippet.position` failed with `HTTP 400: Playlist should use manual sorting to support position.` (reason `manualSortRequired`). Nothing landed (checked by hand). The dispatcher of that time treated the errored insert as possibly landing, reported B in `left_inserted` and stopped. An insert without a position is accepted by such a playlist, and B goes where its sort rule puts it. This is why the [Sorted playlists](#sorted-playlists) mode exists.

## Decisions

1. **Command**: a new `repoint-playlists` (report) with `--apply [--limit N] [--yes]`, shaped like `canonicalize`. Alternative: flags on the existing `playlists` command.
2. **Edit path**: YouTube Data API. Alternative: ytmusicapi playlist edits keyed by `setVideoId`. The probe above showed the API edits this account's playlists, so there was no reason to switch.
3. **Deleted / private A**: report-only. No metadata to run the same-recording check on, and a deleted video can't be inserted again, so a restore is impossible. Alternative: act on them without the check; rejected because the delete is then irreversible.
4. **B already in the playlist, or shown more than once**: report-only. Alternative: a delete-only action (the `unlike_shadow` analog), left for later.
5. **Storage**: reuse `Diagnosis` (`kind = 'playlists'`), `DiagnosisItem` (`playlist_relinked`) and `SyncAttempt`, plus three nullable columns (`diagnoses.playlist_scan_id`, `diagnosis_items.source_item_id` / `related_item_id`), so `issues`, `skip` / `unskip` and `carry_over_skipped` work unchanged. Alternative: separate playlist tables.
6. **Stale scans**: `--apply` exits 1 when a newer playlist scan exists than the report's, and each playlist gets writes only if both live sides equal the scan exactly. Alternative: also refuse the whole run after any earlier playlist write, as `sync` does; it would force a full re-scan between `--limit` slices for playlists nothing touched.
7. **Proof before the delete**: A's item is deleted only after both sides confirm B at A's position, so most failures need only B removed. Alternative: check only after the delete, as likes do; a mismatch would then always depend on re-inserting a region-blocked video.
8. **Order**: playlists with the fewest items first, then by position. Alternative: a `--playlist` filter.
9. **Automatically sorted playlists**: handled without a flag. B goes where the sort order puts it, the checks accept it anywhere with everything else unchanged, and the run names those playlists. Alternatives: skip them (report-only), or have the user switch them to manual sorting first (that changes how their playlist orders new items).

Out of scope: a playable A shown as another video (the user added that video on purpose), saved playlists (the scan gives them no YouTube side, so they produce no findings), any change to likes.

## Known limits

- **A wrong B that renders as itself passes every check.** If YT Music shows B as B, the post-check can't tell a right pair from a wrong one. The same-recording check and the review before `--apply` are the guards; `skip` anything doubtful.
- **Skips carry per playlist.** A skip carries over by (issue type, A, B, playlist), so the same pair in another playlist is a separate finding.
- **Playable A's and delete-only duplicates are out of scope.** Report-only findings stay in the report and are never acted on.
- **Coincidental equal-size gaps.** If YT Music shows an entry the Data API doesn't list, a gap can pair the wrong items while its two sides still have equal counts. Only the same-recording check and the both-sides-before-delete rule guard this.
- **Sorted playlists move B.** In an automatically sorted playlist B ends up at its sort place, not A's index; that is the playlist's own rule. A sort order that reorders other items between our reads, such as popularity, fails the checks and the action is undone.
- **Stale reads.** A YT Music read served from before the insert would pass the post-check, as with the likes repoint's "LM unchanged" check.
