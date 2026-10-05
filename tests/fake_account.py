"""A fake account seen through both ``sync`` clients (YouTube and YT Music).

Models docs/design/ll-lm-alignment.md §2: ``liked`` is YouTube Liked videos
(LL, newest first) and YT Music Liked songs (LM) is derived from it on every
read — each liked video shows as ``renders.get(video, video)``, or not at all
when that is ``None``. A write therefore changes LM the way the rendering
model says, which is what sync's post-action LM check relies on: unliking a
video drops whatever it renders as (a wrong pair drops an unrelated song),
re-liking it brings that entry back on top.
"""

from __future__ import annotations

from typing import Any

from likesurgeon.youtube_client import YouTubeQuotaExceededError, YouTubeWriteError


class FakeAccount:
    def __init__(self, liked: list[str], *, renders: dict[str, str | None] | None = None) -> None:
        self.liked = list(liked)
        self.renders = dict(renders or {})
        # Knobs.
        self.like_does_not_land: set[str] = set()  # rate(v, "like") returns OK, changes nothing
        # rate(v, "none") lands only after the n-th getRating(v) that follows it,
        # even if v is re-liked meanwhile (out-of-order delivery).
        self.unlike_lands_late: dict[str, int] = {}
        self.rate_errors: set[tuple[str, str]] = set()  # rate raises YouTubeWriteError
        self.rate_errors_after_landing: set[tuple[str, str]] = set()  # lands, then raises
        self.rating_errors: set[str] = set()  # getRating raises YouTubeWriteError
        self.quota_after: int | None = None  # YouTube calls beyond this many hit the quota
        self.lm_read_errors: dict[int, BaseException] = {}  # 1-based LM read number -> raised
        self.lm_stale_reads: set[int] = set()  # 1-based LM reads that repeat the previous one
        # Recordings.
        self.rate_calls: list[tuple[str, str]] = []
        self.rating_reads: list[str] = []
        self.lm_reads = 0
        self._youtube_calls = 0
        self._pending_unlikes: dict[str, int] = {}
        self._last_lm_read: list[str] = []

    @property
    def lm(self) -> list[str]:
        return [shown for v in self.liked if (shown := self.renders.get(v, v)) is not None]

    def _youtube_call(self, video_id: str, what: str) -> None:
        self._youtube_calls += 1
        if self.quota_after is not None and self._youtube_calls > self.quota_after:
            raise YouTubeQuotaExceededError(video_id, what, "quotaExceeded")

    # --- YouTubeClient surface -------------------------------------------

    def rate_video(self, video_id: str, rating: str) -> None:
        self.rate_calls.append((video_id, rating))
        self._youtube_call(video_id, rating)
        if (video_id, rating) in self.rate_errors:
            raise YouTubeWriteError(video_id, rating, "boom")
        if rating == "none":
            if video_id in self.unlike_lands_late:
                self._pending_unlikes[video_id] = self.unlike_lands_late[video_id]
            elif video_id in self.liked:
                self.liked.remove(video_id)
        elif video_id not in self.liked and video_id not in self.like_does_not_land:
            self.liked.insert(0, video_id)
        if (video_id, rating) in self.rate_errors_after_landing:
            raise YouTubeWriteError(video_id, rating, "timed out")

    def is_in_liked_videos(self, video_id: str) -> bool:
        self.rating_reads.append(video_id)
        self._youtube_call(video_id, "verify")
        if video_id in self.rating_errors:
            raise YouTubeWriteError(video_id, "verify", "boom")
        liked = video_id in self.liked
        if video_id in self._pending_unlikes:
            self._pending_unlikes[video_id] -= 1
            if not self._pending_unlikes[video_id]:  # lands right after this read
                del self._pending_unlikes[video_id]
                if liked:
                    self.liked.remove(video_id)
        return liked

    # --- YTMusicClient surface -------------------------------------------

    def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.lm_reads += 1
        if (error := self.lm_read_errors.get(self.lm_reads)) is not None:
            raise error
        if self.lm_reads not in self.lm_stale_reads:
            self._last_lm_read = self.lm
        return [{"videoId": v} for v in self._last_lm_read[:limit]]
