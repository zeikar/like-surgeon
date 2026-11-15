"""Pair dead playlist items with the video YT Music shows in their place.

Pure: reads a loaded scan and metadata passed in; no provider calls.

A playlist's YouTube side (``playlistItems.list``) is the store and its YT
Music side renders it in the same order, as LL → LM, so ``align.align`` pairs
them. YT Music shows a playable version (B) in place of a region-blocked item
(A); deleted and private items aren't shown and land in unequal gaps.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .align import align, pair_passes_sanity
from .compare import CanonicalMetadata
from .diagnosis import (
    DIAGNOSIS_KIND_PLAYLISTS,
    ISSUE_PLAYLIST_RELINKED,
    carry_over_skipped,
    same_recording_text,
)
from .models import Diagnosis, DiagnosisItem, Snapshot, SnapshotItem
from .playlists import LoadedScan, load_scan
from .sync import quota_lines
from .youtube_client import playlist_item_key

# One ``playlistItems.list`` call returns up to 50 items.
_PAGE_SIZE = 50
_WRITE_COST = 50


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


def _describe(video_id: str | None, title: str) -> str:
    return f"{video_id} ({title!r})"


def _reason(pair: PlaylistPair, cause: str | None, metadata: dict[str, CanonicalMetadata]) -> str:
    # A dead row can lack a video id; it then has no metadata either.
    a_meta = metadata.get(pair.a.video_id or "")
    b_meta = metadata.get(pair.b.video_id or "")
    sanity = a_meta is not None and b_meta is not None and pair_passes_sanity(a_meta, b_meta)
    parts = [
        f"playlist {pair.playlist.playlist_title!r}, position {pair.a.position + 1}",
        f"A={_describe(pair.a.video_id, pair.a.title)}",
        f"unavailable: {pair.a.unavailable_reason or 'unknown'}",
        f"B={_describe(pair.b.video_id, pair.b.title)}",
        f"same recording: {same_recording_text(sanity, a_meta, b_meta)}",
    ]
    if cause:
        parts.append(f"report-only: {cause}")
    return "; ".join(parts)


def persist_report(
    session: Session,
    scan_id: int,
    pairs: Sequence[PlaylistPair],
    metadata: dict[str, CanonicalMetadata],
) -> tuple[Diagnosis, int]:
    """Write ``pairs`` as a ``playlists`` diagnosis on playlist scan ``scan_id``.

    Returns the diagnosis and how many earlier ``'skipped'`` findings were
    carried over. Carry-over keys on issue type, so likes and canonicalize
    findings never match these.
    """
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=None,
        youtube_snapshot_id=None,
        kind=DIAGNOSIS_KIND_PLAYLISTS,
        playlist_scan_id=scan_id,
    )
    session.add(diagnosis)
    session.flush()
    for pair in pairs:
        cause = report_only_cause(pair, metadata)
        session.add(
            DiagnosisItem(
                diagnosis_id=diagnosis.id,
                issue_type=ISSUE_PLAYLIST_RELINKED,
                confidence=0.5 if cause else 1.0,
                reason=_reason(pair, cause, metadata),
                source_track_id=pair.a.track_id,
                related_track_id=pair.b.track_id,
                source_item_id=pair.a.id,
                related_item_id=pair.b.id,
                status="open",
            )
        )
    session.flush()
    return diagnosis, carry_over_skipped(session, diagnosis)


@dataclass(frozen=True)
class PlaylistAction:
    """Replace dead A with the video B that YT Music shows in its place."""

    item_id: int  # DiagnosisItem id
    playlist_id: str
    playlist_title: str
    a_index: int  # A's position on the YouTube side
    a_playlist_item_id: str
    a_video_id: str
    b_video_id: str
    b_index: int  # B's position on the YT Music side


@dataclass(frozen=True)
class PlaylistState:
    """A playlist as the planning scan saw it, to verify live reads against."""

    youtube: tuple[tuple[str, str | None], ...]  # (playlist item id, video id)
    ytmusic: tuple[str | None, ...]  # video ids


def plan(
    session: Session, diagnosis: Diagnosis
) -> tuple[list[PlaylistAction], dict[str, PlaylistState]]:
    """Open confidence-1.0 findings become actions, plus each planned playlist's state.

    Smallest playlists go first, so a failure in a large one costs the least
    already-done work, then by playlist id and A's position.
    """
    todo = [
        it
        for it in diagnosis.items
        if it.issue_type == ISSUE_PLAYLIST_RELINKED and it.status == "open" and it.confidence == 1.0
    ]
    scan = load_scan(session, diagnosis.playlist_scan_id)
    if not todo or scan is None:
        return [], {}
    ids = {i for it in todo for i in (it.source_item_id, it.related_item_id)}
    items = {
        it.id: it for it in session.scalars(select(SnapshotItem).where(SnapshotItem.id.in_(ids)))
    }
    playlists = {p.ytmusic.id: p for p in scan.playlists}

    actions: list[tuple[int, PlaylistAction]] = []
    for it in todo:
        a, b = items[it.source_item_id], items[it.related_item_id]
        loaded = playlists[b.snapshot_id]
        raw = json.loads(a.raw_json)
        actions.append(
            (
                len(loaded.youtube_items or ()),
                PlaylistAction(
                    item_id=it.id,
                    playlist_id=loaded.ytmusic.playlist_id,
                    playlist_title=loaded.ytmusic.playlist_title,
                    a_index=raw["snippet"]["position"],
                    a_playlist_item_id=raw["id"],
                    a_video_id=a.video_id,
                    b_video_id=b.video_id,
                    b_index=b.position,
                ),
            )
        )
    actions.sort(key=lambda x: (x[0], x[1].playlist_id, x[1].a_index))
    ordered = [a for _, a in actions]
    wanted = {a.playlist_id for a in ordered}
    states = {
        p.ytmusic.playlist_id: PlaylistState(
            youtube=tuple(playlist_item_key(json.loads(i.raw_json)) for i in p.youtube_items or ()),
            ytmusic=tuple(i.video_id for i in p.ytmusic_items),
        )
        for p in scan.playlists
        if p.ytmusic.playlist_id in wanted
    }
    return ordered, states


def _list_calls(items: int) -> int:
    return max(1, -(-items // _PAGE_SIZE))


def summarize(actions: Sequence[PlaylistAction], states: Mapping[str, PlaylistState]) -> str:
    """Human-readable plan summary with the clean-path quota estimate.

    Each action is an insert and a delete plus a read after each; each playlist
    is read once as its baseline. Re-reads, undo calls and the positional
    insert an automatically sorted playlist refuses aren't estimated.
    """
    quota = 0
    for playlist_id in {a.playlist_id for a in actions}:
        quota += _list_calls(len(states[playlist_id].youtube))
    for a in actions:
        n = len(states[a.playlist_id].youtube)
        quota += 2 * _WRITE_COST + _list_calls(n + 1) + _list_calls(n)
    lines = [
        "Playlist repoint plan:",
        f"  Actions: {len(actions)}",
        f"  Playlists: {len({a.playlist_id for a in actions})}",
        *quota_lines(quota),
        "  An automatically sorted playlist refuses the first insert with a position: "
        "one more insert (50 units) per such playlist, not in the estimate",
    ]
    return "\n".join(lines)
