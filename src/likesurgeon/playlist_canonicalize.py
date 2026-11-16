"""Find playable playlist items whose artist page lists another release.

Pure: reads a loaded scan and artist pages passed in; no provider calls.

The likes rule (``canonicalize.find_candidates``) run once per own playlist
that has a YouTube side: A is an audio track YT Music shows in the playlist
that none of its artists' lists contains while exactly one listed track B has
its title key and length (report-only when B's artist ids differ). A must be
shown as itself for a playable YouTube item of its own (``align.align`` per
playlist), so moving A means moving that item. ``playlist_dispatch`` then
repoints A to B (insert B, delete A) or, with B already in the playlist, merges
(delete A).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import canonicalize
from .align import align
from .canonicalize import Candidate, Match, artist_name, performers_cause, version_keyword
from .diagnosis import (
    DIAGNOSIS_KIND_PLAYLIST_CANONICALIZE,
    ISSUE_PLAYLIST_CANONICAL_VERSION,
    carry_over_skipped,
)
from .models import Diagnosis, DiagnosisItem, SnapshotItem, Track
from .playlist_repoint import PlaylistAction, PlaylistState, estimate_quota, playlist_states
from .playlists import LoadedPlaylist, LoadedScan, load_scan
from .snapshot import upsert_ytmusic_track
from .sync import quota_lines
from .ytmusic_client import ArtistSongs


def eligible_playlists(scan: LoadedScan) -> list[LoadedPlaylist]:
    """Own playlists with a YouTube side: only these can be written to and checked."""
    return [p for p in scan.playlists if p.ytmusic.playlist_owned and p.youtube_items is not None]


def raw_tracks(playlist: LoadedPlaylist) -> list[dict[str, Any]]:
    """The YT Music side as the raw ytmusicapi track dicts it was scanned as."""
    return [json.loads(it.raw_json or "{}") for it in playlist.ytmusic_items]


def artist_ids(scan: LoadedScan) -> list[str]:
    """Distinct artist ids of the audio tracks (only those can be an A) over
    every eligible playlist, first-seen order: each artist page is read once
    however many playlists credit it."""
    return canonicalize.artist_ids(
        [
            t
            for p in eligible_playlists(scan)
            for t in raw_tracks(p)
            if t.get("videoType") == canonicalize.ATV
        ]
    )


@dataclass(frozen=True)
class CanonicalPair:
    """Playable A, shown by YT Music as itself in ``playlist``, and its one B."""

    playlist: LoadedPlaylist
    a: SnapshotItem  # A's youtube_playlist row
    shown: SnapshotItem  # A's ytmusic_playlist row: the entry rendering A's item
    candidate: Candidate

    @property
    def b(self) -> Match:
        return self.candidate.matches[0]


@dataclass(frozen=True)
class PlaylistCandidates:
    pairs: tuple[CanonicalPair, ...]
    # Summed over eligible playlists, so an A in two playlists counts twice.
    listed: int  # A already in one of its artists' lists
    unreadable: int  # an artist of A couldn't be read
    ambiguous: int  # two or more B's
    # One B, but A isn't shown as itself for a playable item of its own (dead,
    # availability unknown, or YT Music's rendering of another item).
    not_own_item: int


def find(scan: LoadedScan, artists: Mapping[str, ArtistSongs | None]) -> PlaylistCandidates:
    """The single-B candidates of each eligible playlist whose A is shown as
    itself for a playable YouTube item, plus the counts of the rest."""
    pairs: list[CanonicalPair] = []
    listed = unreadable = ambiguous = not_own = 0
    for playlist in eligible_playlists(scan):
        candidates, summary = canonicalize.find_candidates(raw_tracks(playlist), artists)
        listed += summary.listed
        unreadable += summary.unreadable
        ambiguous += summary.ambiguous
        backings = align(playlist.youtube_items, playlist.ytmusic_items).backings
        for c in candidates:
            if c.ambiguous:
                continue
            vid = c.a["videoId"]
            own = next(
                (
                    b
                    for b in backings
                    if b.lm_item.video_id == vid
                    and b.ll_item is not None
                    and b.ll_item.video_id == vid
                    and b.ll_item.is_available is True
                ),
                None,
            )
            if own is None:
                not_own += 1
                continue
            pairs.append(
                CanonicalPair(
                    playlist=playlist,
                    a=own.ll_item,
                    shown=playlist.ytmusic_items[own.lm_index],
                    candidate=c,
                )
            )
    return PlaylistCandidates(
        pairs=tuple(pairs),
        listed=listed,
        unreadable=unreadable,
        ambiguous=ambiguous,
        not_own_item=not_own,
    )


def structural_cause(playlist: LoadedPlaylist, a_video_id: str, b_video_id: str) -> str | None:
    """Why the pair is report-only from the playlist's shape, or None: A in it
    more than once on either side, or B not either absent from both sides or
    listed once and shown once (B shown only as another item's rendering, or
    listed but hidden, stands for something a check can't follow), or B's item
    not known to play (a merge would keep it and delete the playable A)."""
    youtube = Counter(it.video_id for it in playlist.youtube_items or ())
    ytmusic = Counter(it.video_id for it in playlist.ytmusic_items)
    causes = []
    if youtube[a_video_id] > 1 or ytmusic[a_video_id] > 1:
        causes.append(
            f"A listed {youtube[a_video_id]} times on the YouTube side, "
            f"shown {ytmusic[a_video_id]} times by YT Music"
        )
    if (youtube[b_video_id], ytmusic[b_video_id]) not in ((0, 0), (1, 1)):
        causes.append(
            f"B listed {youtube[b_video_id]} times on the YouTube side, "
            f"shown {ytmusic[b_video_id]} times by YT Music"
        )
    elif any(
        it.video_id == b_video_id and it.is_available is not True
        for it in playlist.youtube_items or ()
    ) or any(
        it.video_id == b_video_id and json.loads(it.raw_json or "{}").get("isAvailable") is False
        for it in playlist.ytmusic_items
    ):
        causes.append("B's item in the playlist isn't playable")
    return "; ".join(causes) or None


def _keyword(shown: SnapshotItem) -> str | None:
    return version_keyword(shown.album or "")


def _keyword_cause(keyword: str) -> str:
    return f"version keyword '{keyword}' in A's album"


def report_only_cause(pair: CanonicalPair) -> str | None:
    """Why ``pair`` must stay report-only, or None if it may be applied."""
    keyword = _keyword(pair.shown)
    causes = [
        _keyword_cause(keyword) if keyword else None,
        performers_cause(pair.candidate.a, pair.b.song),
        structural_cause(pair.playlist, pair.a.video_id or "", pair.b.song["videoId"]),
    ]
    return "; ".join(c for c in causes if c) or None


def _reason(pair: CanonicalPair, cause: str | None) -> str:
    b = pair.b
    in_playlist = any(it.video_id == b.song["videoId"] for it in pair.playlist.youtube_items or ())
    parts = [
        f"playlist {pair.playlist.ytmusic.playlist_title!r}, position {pair.a.position + 1}",
        f"A={canonicalize.describe(pair.candidate.a['videoId'], pair.candidate.a)}",
        f"B={canonicalize.describe(b.song['videoId'], b.song)}",
        f"artist {artist_name(b.artist_id, pair.candidate.a, b.song)!r} lists B",
        f"B already in the playlist: {'yes' if in_playlist else 'no'}",
    ]
    if cause:
        parts.append(f"report-only: {cause}")
    return "; ".join(parts)


def persist_report(
    session: Session, scan_id: int, pairs: Sequence[CanonicalPair]
) -> tuple[Diagnosis, int]:
    """Write ``pairs`` as a ``playlist_canonicalize`` diagnosis on playlist scan
    ``scan_id``. Returns the diagnosis and how many earlier ``'skipped'``
    findings were carried over (same issue type, tracks and playlist only)."""
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=None,
        youtube_snapshot_id=None,
        kind=DIAGNOSIS_KIND_PLAYLIST_CANONICALIZE,
        playlist_scan_id=scan_id,
    )
    session.add(diagnosis)
    session.flush()
    for pair in pairs:
        cause = report_only_cause(pair)
        if performers_cause(pair.candidate.a, pair.b.song):
            confidence = canonicalize.CONFIDENCE_DIFFERENT_PERFORMERS
        else:
            confidence = 0.5 if cause else 1.0
        session.add(
            DiagnosisItem(
                diagnosis_id=diagnosis.id,
                issue_type=ISSUE_PLAYLIST_CANONICAL_VERSION,
                confidence=confidence,
                reason=_reason(pair, cause),
                source_track_id=pair.a.track_id,
                related_track_id=upsert_ytmusic_track(session, pair.b.song).id,
                source_item_id=pair.a.id,
                related_item_id=pair.shown.id,
                status="open",
            )
        )
    session.flush()
    return diagnosis, carry_over_skipped(session, diagnosis)


def plan(
    session: Session, diagnosis: Diagnosis, *, include_flagged: bool
) -> tuple[list[PlaylistAction], dict[str, PlaylistState]]:
    """Open confidence-1.0 findings become actions, plus each planned
    playlist's state; with ``include_flagged`` also a 0.5 finding whose only
    cause is a version keyword in A's album (causes are recomputed from the
    report's scan and B's ``Track``, the song dict the report stored, never
    read from ``reason``).

    Order as ``repoint-playlists``: smallest playlist, playlist id, A's
    position. The kind is a forecast (the dispatcher decides from its live
    baseline): a merge when B is on the playlist's YouTube side in the scan or
    an earlier action of this plan inserts it there, else a repoint.
    """
    todo = [
        it
        for it in diagnosis.items
        if it.issue_type == ISSUE_PLAYLIST_CANONICAL_VERSION
        and it.status == "open"
        and (it.confidence == 1.0 or (include_flagged and it.confidence == 0.5))
    ]
    scan = load_scan(session, diagnosis.playlist_scan_id)
    if not todo or scan is None:
        return [], {}
    item_ids = {i for it in todo for i in (it.source_item_id, it.related_item_id)}
    rows = {
        r.id: r for r in session.scalars(select(SnapshotItem).where(SnapshotItem.id.in_(item_ids)))
    }
    b_tracks = {
        t.id: t
        for t in session.scalars(
            select(Track).where(Track.id.in_({it.related_track_id for it in todo}))
        )
    }
    playlists = {p.ytmusic.id: p for p in scan.playlists}

    keyed: list[tuple[tuple[int, str, int], PlaylistAction, LoadedPlaylist]] = []
    for it in todo:
        a, shown = rows[it.source_item_id], rows[it.related_item_id]
        loaded = playlists[shown.snapshot_id]
        b = b_tracks[it.related_track_id]
        b_video_id = b.video_id
        # Every finding, 1.0 too: a report saved before the performer check
        # has no 0.4 to tell it apart.
        if (
            performers_cause(json.loads(shown.raw_json or "{}"), json.loads(b.raw_json or "{}"))
            is not None
        ):
            continue
        if it.confidence != 1.0 and (
            _keyword(shown) is None
            or structural_cause(loaded, a.video_id or "", b_video_id or "") is not None
        ):
            continue
        raw = json.loads(a.raw_json or "{}")
        action = PlaylistAction(
            item_id=it.id,
            playlist_id=loaded.ytmusic.playlist_id,
            playlist_title=loaded.ytmusic.playlist_title,
            a_index=raw["snippet"]["position"],
            a_playlist_item_id=raw["id"],
            a_video_id=a.video_id,
            b_video_id=b_video_id,
            b_index=shown.position,
        )
        key = (len(loaded.youtube_items or ()), action.playlist_id, action.a_index)
        keyed.append((key, action, loaded))
    keyed.sort(key=lambda x: x[0])

    # (playlist id, B) left in the playlist by an earlier action of this plan.
    planned_b: set[tuple[str, str]] = set()
    actions: list[PlaylistAction] = []
    for _, action, loaded in keyed:
        listed = any(i.video_id == action.b_video_id for i in loaded.youtube_items or ())
        there = listed or (action.playlist_id, action.b_video_id) in planned_b
        planned_b.add((action.playlist_id, action.b_video_id))
        kind = "canonical_merge" if there else "canonical_repoint"
        actions.append(replace(action, kind=kind))
    return actions, playlist_states(scan, {a.playlist_id for a in actions})


def summarize(actions: Sequence[PlaylistAction], states: Mapping[str, PlaylistState]) -> str:
    """Human-readable plan summary with the clean-path quota estimate."""
    by_kind = Counter(a.kind for a in actions)
    lines = [
        "Playlist canonicalize plan:",
        f"  canonical_repoint: {by_kind['canonical_repoint']}",
        f"  canonical_merge: {by_kind['canonical_merge']}",
        f"  Playlists: {len({a.playlist_id for a in actions})}",
    ]
    if by_kind["canonical_repoint"]:
        lines.append(
            "  note: a repoint puts B in A's place; in an automatically sorted playlist, "
            "where the playlist's sort order puts it"
        )
    if by_kind["canonical_merge"]:
        lines.append("  note: each merge removes one entry from its playlist")
    lines += quota_lines(estimate_quota(actions, states))
    if by_kind["canonical_repoint"]:  # a merge inserts nothing
        lines.append(
            "  An automatically sorted playlist refuses the first insert with a position: "
            "one more insert (50 units) per such playlist, not in the estimate"
        )
    return "\n".join(lines)
