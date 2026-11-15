"""Pair dead playlist items with the video YT Music shows in their place.

Pure: reads a loaded scan and metadata passed in; no provider calls.

A playlist's YouTube side (``playlistItems.list``) is the store and its YT
Music side renders it in the same order, as LL → LM, so ``align.align`` pairs
them. YT Music shows a playable version (B) in place of a region-blocked item
(A); deleted and private items aren't shown and land in unequal gaps.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .align import align, pair_passes_sanity
from .compare import CanonicalMetadata
from .models import Snapshot, SnapshotItem
from .playlists import LoadedScan


@dataclass(frozen=True)
class PlaylistPair:
    """Dead YouTube item ``a`` shown by YT Music as ``b`` in ``playlist``."""

    playlist: Snapshot  # the YT Music side's snapshot (id, title, owned)
    a: SnapshotItem  # youtube_playlist row
    b: SnapshotItem  # ytmusic_playlist row
    b_youtube_count: int  # how often B is listed on the YouTube side
    b_ytmusic_count: int  # how often YT Music shows B


@dataclass(frozen=True)
class PlaylistPairs:
    pairs: tuple[PlaylistPair, ...]
    dead_shown_as_itself: int
    # Dead items backing no YT Music entry: in an unequal gap (including one YT
    # Music shows nothing for: deleted / private) or left ambiguous by alignment.
    dead_unplaced: int


def find_pairs(scan: LoadedScan) -> PlaylistPairs:
    """Pair every dead YouTube item that YT Music shows as another video."""
    pairs: list[PlaylistPair] = []
    shown_as_itself = unplaced = 0
    for playlist in scan.playlists:
        # Without the YouTube side there is no availability to go on.
        if playlist.youtube_items is None:
            continue
        result = align(playlist.youtube_items, playlist.ytmusic_items)
        youtube_counts = Counter(i.video_id for i in playlist.youtube_items)
        ytmusic_counts = Counter(i.video_id for i in playlist.ytmusic_items)
        for backing in result.backings:
            a = backing.ll_item
            if a is None or a.is_available is not False:
                continue
            b = backing.lm_item
            if b.video_id == a.video_id:
                shown_as_itself += 1
                continue
            pairs.append(
                PlaylistPair(
                    playlist=playlist.ytmusic,
                    a=a,
                    b=b,
                    b_youtube_count=youtube_counts[b.video_id],
                    b_ytmusic_count=ytmusic_counts[b.video_id],
                )
            )
        unplaced += sum(
            1
            for i, it in enumerate(playlist.youtube_items)
            if it.is_available is False and i not in result.rendered_ll
        )
    return PlaylistPairs(
        pairs=tuple(pairs), dead_shown_as_itself=shown_as_itself, dead_unplaced=unplaced
    )


def report_only_cause(pair: PlaylistPair, metadata: dict[str, CanonicalMetadata]) -> str | None:
    """Why ``pair`` must stay report-only, or ``None`` if it may be repointed."""
    if pair.playlist.playlist_owned is None:
        return "playlist ownership unknown"
    if not pair.playlist.playlist_owned:
        return "saved playlist"
    # Repointing would leave B listed twice; removing A alone is a different action.
    if pair.b_youtube_count:
        return "B is also on the playlist's YouTube side"
    if pair.b_ytmusic_count != 1:
        return f"YT Music shows B {pair.b_ytmusic_count} times"
    a_meta = metadata.get(pair.a.video_id)
    b_meta = metadata.get(pair.b.video_id)
    # Checked before the same-recording check, which also fails on missing metadata.
    if a_meta is None:
        return "no videos.list metadata for A"
    if b_meta is None:
        return "no videos.list metadata for B"
    if not pair_passes_sanity(a_meta, b_meta):
        return "same-recording check failed"
    return None
