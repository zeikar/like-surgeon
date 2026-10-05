"""Helpers shared across the alignment pipeline.

The old three-stage cross-source matcher (video_id / canonical_key / fuzzy) was
removed when ``compare-likes`` moved to LL->LM order alignment (``align.py``).
What remains is ``dedupe_by_video_id`` (used by metadata-drift detection and
``doctor``) and ``CanonicalMetadata`` (the ``videos.list`` record used by
``youtube_client`` and ``align``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar


class _VideoIdPositioned(Protocol):
    """Minimal duck-typed contract for ``dedupe_by_video_id``.

    Keeps ``compare.py`` decoupled from the SQLAlchemy ORM — both real
    ``SnapshotItem`` rows and lightweight test stand-ins satisfy this.
    """

    video_id: str | None
    position: int


_T = TypeVar("_T", bound=_VideoIdPositioned)


def dedupe_by_video_id(items: Sequence[_T]) -> list[_T]:
    """Return one row per ``video_id`` (lowest-position winner), NULL-safe.

    Contract:
      - Rows with falsy ``video_id`` pass through unchanged (no identity).
      - For each non-null ``video_id`` group, keep the row with the
        smallest ``position``; discard the rest.
      - Output is sorted by ``position`` ascending (position-stable).
      - Idempotent: running twice returns an equal list.
    """
    kept: dict[str, _T] = {}
    no_id: list[_T] = []
    for it in items:
        if not it.video_id:
            no_id.append(it)
            continue
        existing = kept.get(it.video_id)
        if existing is None or it.position < existing.position:
            kept[it.video_id] = it
    combined: list[_T] = list(kept.values()) + no_id
    combined.sort(key=lambda x: x.position)
    return combined


@dataclass(frozen=True)
class CanonicalMetadata:
    """Authoritative metadata for a YouTube video, fetched via videos.list.

    Distinct from playlistItems.list data — at the videos.list endpoint,
    snippet.channelId IS the video uploader (there's no videoOwnerChannelId
    field). The playlistItems-side equivalent is snippet.videoOwnerChannelId
    (snippet.channelId there is the playlist owner, NOT the uploader).
    """

    video_id: str
    title: str
    channel_id: str
    duration_seconds: int
