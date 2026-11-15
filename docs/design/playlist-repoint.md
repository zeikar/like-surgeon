# Playlist repoint

Spec for `repoint-playlists`: a report of dead items in your own playlists that YT Music shows as another video (A → B), and `repoint-playlists --apply`, which replaces A's playlist item with B at the same position through the YouTube Data API. The code is in `playlist_repoint.py` (pure pairing, report, planner) and `playlist_dispatch.py` (the writes); this is the model and the rules they follow.

## Model

A playlist is one store that YT Music renders, as LL → LM is for likes: the **YouTube side** (`playlistItems.list`, each item has an item id and a video id) is the store, and the **YT Music side** (`get_playlist`) shows the same entries in the same order, with a playable version (B) in place of a region-blocked item (A). Deleted and private items aren't shown at all. So the pairing is `align.align(youtube side, ytmusic side)` run per playlist, on the stored scan: anchors are entries whose video id is on both sides, and the gaps between them are paired in order when their sizes are equal. Playlists are independent; nothing crosses from one to another.

A repoint is not a new item replacing an old one in the store's eyes: A's item is removed and B's item put at the same index, so the YouTube side changes and the YT Music side must read exactly as before.

## The finding

`playlist_relinked`: a dead A on the YouTube side that alignment pairs with a B whose video id differs. Stored in a `Diagnosis` with `kind = 'playlists'` (`playlist_scan_id` = the scan it was built on), one `DiagnosisItem` per pair, with `source_item_id` = A's `youtube_playlist` row and `related_item_id` = B's `ytmusic_playlist` row (`source_track_id` / `related_track_id` as for likes).

Confidence 1.0 is write-eligible; 0.5 is report-only, and the reason says which cause (`report_only_cause`, first match wins):

- ownership of the playlist unknown, or a saved (not owned) playlist
- B is also on the playlist's YouTube side (YT Music shows B twice)
- YT Music shows B other than once
- no `videos.list` metadata for A, or for B
- the same-recording check fails (duration within ±3 s and same channel or overlapping titles)

Dead items that never become findings are counted in the report: ones YT Music shows as themselves, and ones it doesn't place (deleted and private items sit in unequal gaps, as does any gap alignment can't pair). Real-account numbers from the first run: 35 playlists, 199 dead items, 45 findings of which 43 are write-eligible (2 have B already on the YouTube side), 28 dead items shown as themselves, 126 not placed. All 45 had a region-blocked A.

## Action flow

For A at YouTube index *p* (item id *a*), shown by YT Music as B at index *j*, in playlist P. Every write is committed to `sync_attempts` at once, before any wait. The module docstring of `playlist_dispatch.py` is the authority; this is its summary.

0. **Baseline**: the previous action's matching post-check reads of P, else fresh reads of both sides that must equal the scan plus this run's applied repoints, with A and B where the action says (row `playlist_precheck`). A difference means no writes in P this run: its items stay `open` and other playlists continue. An unreadable side stops the run.
1. **Insert B at *p*** (`playlist_insert`). Wait 5 s. The YouTube side must be the baseline with B inserted at *p* (`playlist_insert_verify`); YT Music must be the baseline with one more B next to *j* (`playlist_ytm_verify`). Each check re-reads once after 15 s on a mismatch.
2. **Delete item *a*** (`playlist_delete`; anything stopping the action from here on leaves a `playlist_interrupted` row). Wait 5 s. The YouTube side must be the baseline with slot *p* holding B (`playlist_delete_verify`); YT Music must equal the baseline, order included (`playlist_check`). Both match: `applied`, and these reads are P's next baseline.
3. **Undo**. B is removed (`playlist_rollback`, `playlist_rollback_verify`) only while A's item is known to be in place: before the delete call, when the quota rejected the delete, or after A was put back. After a delete that was sent:
   - The YouTube side can't confirm item *a* gone (still listed after the re-read, or unreadable): nothing is undone. The delete may still land, so B stays and the pair is reported (`delete_unconfirmed`).
   - Item *a* is gone but YT Music doesn't match or can't be read: A is inserted again at *p* (`playlist_restore`, `playlist_restore_verify`: A back at *p* under a new item id, B's own item right after it, and every other item unchanged, item ids included), and only once that is confirmed is B removed. If A can't be put back, B stays in its slot and A is reported (`left_removed`).
   - A B that can't be removed or confirmed gone is reported (`left_inserted`).

A repoint keeps the playlist's length and every other slot, so a later action's indices in the same playlist still hold.

| Case | Item status | Run |
|---|---|---|
| Every call succeeded, both checks pass | `applied` | continues |
| Delete errored but landed, both checks pass | `open` (counted `failed`) | continues |
| YT Music didn't show the inserted B; B removed again | `skipped` | stops |
| A was put back after the delete (restored) | `skipped` | stops |
| Quota rejected the insert (nothing sent) | `open` | stops (quota) |
| Quota rejected the delete; B removed again | `open` | stops (quota) |
| Insert errored and didn't land, or landed at A's index and was removed | `open` | continues |
| Insert errored and landed elsewhere | `open`, B in `left_inserted` or removed | stops |
| 2xx insert not listed after the re-read | `open`, B in `left_inserted` | stops |
| YouTube side after the insert isn't the expected list; B removed | `open` | stops |
| Delete not confirmed | `open`, `delete_unconfirmed` | stops |
| A couldn't be put back | `open`, `left_removed` | stops |
| B couldn't be removed | `open`, `left_inserted` | stops |
| A side unreadable | `open` | stops |
| Live playlist differs from the scan | `open` | continues; that playlist only |

`skipped` is terminal (`unskip` re-opens it). Everything else stays `open`. The CLI exits 1 when any action failed, the run stopped early, the quota ran out or a playlist got no writes.

## Row kinds

All `SyncAttempt.kind` values start with `playlist_`.

- **Writes**: `playlist_insert`, `playlist_delete`, `playlist_rollback` (removes B), `playlist_restore` (puts A back), and `playlist_interrupted` (a marker, also on Ctrl-C, after the delete call started).
- **Reads and checks**: `playlist_precheck`, `playlist_insert_verify`, `playlist_ytm_verify`, `playlist_delete_verify`, `playlist_check`, `playlist_rollback_verify`, `playlist_restore_verify`.

Only an applied `playlist_check` (B in, A out, both sides confirmed) or an applied `playlist_rollback_verify` (B out, A in place) settles an action; every other row may leave the playlist changed or half-changed. `DiagnosisItem.reason` is never modified.

## Operating rules

- **The report is built on the latest scan.** `--apply` exits 1 when a newer playlist scan exists than the report's. Re-run `repoint-playlists` after `scan playlists`.
- **Per-playlist live check.** Each playlist's first action reads both sides and compares them with the scan; any difference means no writes in that playlist this run. Later actions reuse the previous action's post-check reads. There is no run-wide refusal after earlier playlist writes, so `--limit` slices don't force a re-scan between them; the report is stale after any run, though, and the next `--apply` needs a fresh scan and report.
- **Order.** Playlists with the fewest items first, then by playlist id and A's position, so `--limit 1` is the smallest possible first write.
- **`--limit N`** caps findings per run. **`--yes`** skips the prompt.
- **Quota.** Insert and delete cost 50 units each, `playlistItems.list` 1 per page of 50. The plan's estimate is, per playlist, one read of its YouTube side, and per action 2 writes plus a read with one more item and one with the original count (about 100 units plus the reads). Re-reads, undo calls (50 each) and YT Music reads (free) aren't estimated. Running out stops the run after the action in flight settles; exit 1.
- **Unsettled actions.** `repoint-playlists --apply` lists, per playlist title, position, A and B, every action with a write row after its last settling row. An entry drops off once a YT Music and a YouTube scan of that playlist are both newer than its last write row. Playlist rows are not in `sync_preflight`'s `_WRITE_KINDS` or `_SETTLING_KINDS`: they never block `sync`, and they never count toward the stranded-likes list.
- **YT Music reads refresh stale cookies** once (`YTMusicClient.fetch_playlist`), as the likes checks do.

## Evidence

A live probe on 2026-11-15, on an owned 16-item playlist of an account that has no YouTube channel. The probe added two items and removed exactly those two. Nothing that was already in the playlist was moved or deleted, and both sides matched their pre-probe state afterwards.

- **Positional insert works.** `playlistItems.insert` with `snippet.position = 1` succeeded on the owned playlist even though the account has no channel. The new item sat at index 1, and every other item id kept its order.
- **YT Music reflects edits fast.** The inserted video showed in the YT Music playlist at the same index after about 0.6 s, and the rest of the YT Music list was unchanged. After the delete, the YT Music list was back to its original state after about 0.5 s.
- **The YouTube side can lag right after a delete.** One `playlistItems.list` read taken about a second after `playlistItems.delete` still differed from the original list. A read a few seconds later matched. Verification after a delete therefore needs a wait and a re-read before it treats a mismatch as real.
- **A region-blocked video can be inserted again.** The API accepted an insert of the playlist's own region-blocked video at index 1. YT Music showed, in that slot, the same playable video it already shows in place of that blocked video elsewhere in the playlist. The list grew by one and the rest was unchanged. This confirms the rendering model (YT Music shows a playable version in place of a blocked item), and that putting a region-blocked A back during an undo is possible.
- **Quota.** Taken from the documented costs, not measured: 50 units per insert or delete, 1 per `playlistItems.list` page.

## Decisions

1. **Command**: a new `repoint-playlists` (report) with `--apply [--limit N] [--yes]`, shaped like `canonicalize`. Alternative: flags on the existing `playlists` command.
2. **Edit path**: YouTube Data API. Alternative: ytmusicapi playlist edits keyed by `setVideoId`. The probe above showed the API edits this account's playlists, so there was no reason to switch.
3. **Deleted / private A**: report-only. No metadata to run the same-recording check on, and a deleted video can't be inserted again, so a restore is impossible. Alternative: act on them without the check; rejected because the delete is then irreversible.
4. **B already in the playlist, or shown more than once**: report-only. Alternative: a delete-only action (the `unlike_shadow` analog), left for later.
5. **Storage**: reuse `Diagnosis` (`kind = 'playlists'`), `DiagnosisItem` (`playlist_relinked`) and `SyncAttempt`, plus three nullable columns (`diagnoses.playlist_scan_id`, `diagnosis_items.source_item_id` / `related_item_id`), so `issues`, `skip` / `unskip` and `carry_over_skipped` work unchanged. Alternative: separate playlist tables.
6. **Stale scans**: `--apply` exits 1 when a newer playlist scan exists than the report's, and each playlist gets writes only if both live sides equal the scan exactly. Alternative: also refuse the whole run after any earlier playlist write, as `sync` does; it would force a full re-scan between `--limit` slices for playlists nothing touched.
7. **Proof before the delete**: A's item is deleted only after both sides confirm B at A's position, so most failures need only B removed. Alternative: check only after the delete, as likes do; a mismatch would then always depend on re-inserting a region-blocked video.
8. **Order**: playlists with the fewest items first, then by position. Alternative: a `--playlist` filter.

Out of scope: a playable A shown as another video (the user added that video on purpose), saved playlists (findings are stored report-only), any change to likes.

## Known limits

- **A wrong B that renders as itself passes every check.** If YT Music shows B as B, the post-check can't tell a right pair from a wrong one. The same-recording check and the review before `--apply` are the guards; `skip` anything doubtful.
- **Skips carry across playlists.** A skip carries over by (issue type, A, B), so it also covers the same pair in another playlist.
- **Playable A's and delete-only duplicates are out of scope.** Report-only findings stay in the report and are never acted on.
- **Coincidental equal-size gaps.** If YT Music shows an entry the Data API doesn't list, a gap can pair the wrong items while its two sides still have equal counts. Only the same-recording check and the both-sides-before-delete rule guard this.
- **Stale reads.** A YT Music read served from before the insert would pass the post-check, as with the likes repoint's "LM unchanged" check.
