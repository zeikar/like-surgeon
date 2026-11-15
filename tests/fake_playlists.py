"""A fake account's playlists seen through both playlist-repoint clients.

Models docs/design/playlist-repoint.md: ``playlists`` holds each playlist's
YouTube side, ``(playlist item id, video id)`` in order, and the YT Music side
is derived from it on every read — each item shows as
``renders.get(video, video)``, or not at all when that is ``None``. Inserting
a blocked A again therefore shows its B in that slot, as seen live.

A playlist in ``auto_sorted`` is sorted automatically: like the live API, it
refuses an insert with a position (HTTP 400, ``PlaylistManualSortRequired``)
and puts an insert without one where its rule says, ``"top"`` (newest first)
or ``"end"`` (oldest first). A manually sorted playlist appends it.
"""

from __future__ import annotations

from itertools import count
from typing import Any

from likesurgeon.youtube_client import (
    PlaylistManualSortRequired,
    YouTubeQuotaExceededError,
    YouTubeWriteError,
)


class FakePlaylistAccount:
    def __init__(
        self,
        playlists: dict[str, list[tuple[str, str]]],
        *,
        renders: dict[str, str | None] | None = None,
    ) -> None:
        self.playlists = {pid: list(items) for pid, items in playlists.items()}
        self.renders = dict(renders or {})
        # Knobs. Inserts key on the video id, deletes on the playlist item id.
        self.insert_does_not_land: set[str] = set()  # returns a new item id, changes nothing
        self.insert_errors: set[str] = set()  # can't be inserted: raises, nothing lands
        self.insert_errors_after_landing: set[str] = set()  # lands, then raises
        self.insert_lands_at_end: set[str] = set()  # position ignored
        self.auto_sorted: dict[str, str] = {}  # playlist id -> "top" | "end"
        self.delete_does_not_land: set[str] = set()  # returns OK, changes nothing
        self.delete_errors: set[str] = set()  # raises, nothing lands
        self.delete_errors_after_landing: set[str] = set()  # lands, then raises
        # Lands right after the n-th YouTube read of its playlist that follows it.
        self.delete_lands_late: dict[str, int] = {}
        # Lands at once (YT Music shows it), but the next n YouTube reads of its
        # playlist still list the item: the read lag seen live after a delete.
        self.delete_read_lag: dict[str, int] = {}
        self.quota_after: int | None = None  # YouTube calls beyond this many hit the quota
        self.youtube_read_errors: dict[int, BaseException] = {}  # 1-based read number -> raised
        self.ytm_read_errors: dict[int, BaseException] = {}  # 1-based read number -> raised
        # Reads that repeat that playlist's previous one (if any).
        self.ytm_stale_reads: set[int] = set()
        self.ytm_reads_returning: dict[int, list[str | None]] = {}  # reads that return this list
        # Recordings.
        self.write_calls: list[tuple[str, ...]] = []
        self.youtube_reads = 0
        self.ytm_reads = 0
        self._youtube_calls = 0
        self._new_ids = count(1)
        self._pending_deletes: dict[str, int] = {}
        self._last_ytm_read: dict[str, list[str | None]] = {}
        self._lagging_reads: dict[str, tuple[list[tuple[str, str]], int]] = {}

    def ytmusic(self, playlist_id: str) -> list[str | None]:
        return [
            shown
            for _, v in self.playlists[playlist_id]
            if (shown := self.renders.get(v, v)) is not None
        ]

    def _youtube_call(self, subject: str, op: str) -> None:
        self._youtube_calls += 1
        if self.quota_after is not None and self._youtube_calls > self.quota_after:
            raise YouTubeQuotaExceededError(subject, op, "quotaExceeded")

    def _remove(self, item_id: str) -> None:
        for pid, items in self.playlists.items():
            if item_id in self.delete_read_lag and any(i == item_id for i, _ in items):
                self._lagging_reads[pid] = (list(items), self.delete_read_lag[item_id])
            items[:] = [it for it in items if it[0] != item_id]

    # --- YouTubeClient surface -------------------------------------------

    def insert_playlist_item(
        self, playlist_id: str, video_id: str, position: int | None = None
    ) -> str:
        self._youtube_call(video_id, "playlist_insert")  # quota-rejected: not recorded as sent
        # An insert without a position is recorded without one.
        self.write_calls.append(
            ("insert", playlist_id, video_id)
            if position is None
            else ("insert", playlist_id, video_id, str(position))
        )
        rule = self.auto_sorted.get(playlist_id)
        if rule is not None and position is not None:
            raise PlaylistManualSortRequired(
                video_id,
                "playlist_insert",
                "HTTP 400: Playlist should use manual sorting to support position.",
            )
        if video_id in self.insert_errors:
            raise YouTubeWriteError(video_id, "playlist_insert", "HTTP 403: forbidden")
        item_id = f"new{next(self._new_ids)}"
        if video_id not in self.insert_does_not_land:
            items = self.playlists[playlist_id]
            if video_id in self.insert_lands_at_end or (position is None and rule != "top"):
                at = len(items)
            else:
                at = 0 if position is None else position
            items.insert(at, (item_id, video_id))
        if video_id in self.insert_errors_after_landing:
            raise YouTubeWriteError(video_id, "playlist_insert", "timed out")
        return item_id

    def delete_playlist_item(self, item_id: str) -> None:
        self._youtube_call(item_id, "playlist_delete")
        self.write_calls.append(("delete", item_id))
        if item_id in self.delete_errors:
            raise YouTubeWriteError(item_id, "playlist_delete", "HTTP 500: backend error")
        if item_id in self.delete_lands_late:
            self._pending_deletes[item_id] = self.delete_lands_late[item_id]
        elif item_id not in self.delete_does_not_land:
            self._remove(item_id)
        if item_id in self.delete_errors_after_landing:
            raise YouTubeWriteError(item_id, "playlist_delete", "timed out")

    def read_playlist_items(self, playlist_id: str) -> list[tuple[str, str | None]]:
        self.youtube_reads += 1
        self._youtube_call(playlist_id, "playlist_read")
        if (error := self.youtube_read_errors.get(self.youtube_reads)) is not None:
            raise error
        if playlist_id in self._lagging_reads:
            before, left = self._lagging_reads.pop(playlist_id)
            if left > 1:
                self._lagging_reads[playlist_id] = (before, left - 1)
            return list(before)
        items = list(self.playlists[playlist_id])
        for item_id in [i for i, _ in items if i in self._pending_deletes]:
            self._pending_deletes[item_id] -= 1
            if not self._pending_deletes[item_id]:  # lands right after this read
                del self._pending_deletes[item_id]
                self._remove(item_id)
        return items

    # --- YTMusicClient surface -------------------------------------------

    def fetch_playlist(self, playlist_id: str) -> dict[str, Any]:
        self.ytm_reads += 1
        if (error := self.ytm_read_errors.get(self.ytm_reads)) is not None:
            raise error
        if (given := self.ytm_reads_returning.get(self.ytm_reads)) is not None:
            shown = given
        elif self.ytm_reads in self.ytm_stale_reads and playlist_id in self._last_ytm_read:
            shown = self._last_ytm_read[playlist_id]
        else:
            shown = self.ytmusic(playlist_id)
        self._last_ytm_read[playlist_id] = shown
        return {"id": playlist_id, "tracks": [{"videoId": v} for v in shown]}
