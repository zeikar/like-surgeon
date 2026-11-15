# Playlist repoint

## Evidence

A live probe on 2026-11-15, on an owned 16-item playlist of an account that has no YouTube channel. The probe added two items and removed exactly those two. Nothing that was already in the playlist was moved or deleted, and both sides matched their pre-probe state afterwards.

- **Positional insert works.** `playlistItems.insert` with `snippet.position = 1` succeeded on the owned playlist even though the account has no channel. The new item sat at index 1, and every other item id kept its order.
- **YT Music reflects edits fast.** The inserted video showed in the YT Music playlist at the same index after about 0.6 s, and the rest of the YT Music list was unchanged. After the delete, the YT Music list was back to its original state after about 0.5 s.
- **The YouTube side can lag right after a delete.** One `playlistItems.list` read taken about a second after `playlistItems.delete` still differed from the original list. A read a few seconds later matched. Verification after a delete therefore needs a wait and a re-read before it treats a mismatch as real.
- **A region-blocked video can be inserted again.** The API accepted an insert of the playlist's own region-blocked video at index 1. YT Music showed, in that slot, the same playable video it already shows in place of that blocked video elsewhere in the playlist. The list grew by one and the rest was unchanged. This confirms the rendering model (YT Music shows a playable version in place of a blocked item), and that putting a region-blocked A back during an undo is possible.
- **Quota.** Taken from the documented costs, not measured: 50 units per insert or delete, 1 per `playlistItems.list` page.
