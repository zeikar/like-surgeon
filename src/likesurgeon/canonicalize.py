"""Canonicalize: find liked tracks whose artist page lists another version.

Pure discovery. See ``docs/design/canonicalize.md`` §2 and §4.

A (a liked ATV) is a candidate when none of its artists' songs lists (Show all
or Top songs) contains it while exactly one ATV B in the Show-all lists has the
same title key and a duration within 1 s. This is the one place a title picks the
target of a write: only inside A's own artist lists, only by exact key
equality — A's like is A's own, never inferred. (``align.pair_passes_sanity``
compares titles too, but only to verify pairs that order has already fixed.)
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .diagnosis import (
    DIAGNOSIS_KIND_CANONICALIZE,
    ISSUE_CANONICAL_AMBIGUOUS,
    ISSUE_CANONICAL_VERSION,
    carry_over_skipped,
)
from .models import Diagnosis, DiagnosisItem, SnapshotItem
from .normalize import normalize_for_match
from .snapshot import upsert_ytmusic_track
from .sync import QUOTA_COST, PlannedAction, quota_lines
from .ytmusic_client import ArtistSongs

ATV = "MUSIC_VIDEO_TYPE_ATV"
DURATION_TOLERANCE_S = 1

# Album words that mark a possibly different recording with the same title and
# length (§4.2). A hit makes the finding report-only.
VERSION_KEYWORDS: tuple[str, ...] = (
    "Version",
    "Ver.",
    "Live",
    "Remix",
    "Acoustic",
    "Instrumental",
    "Off Vocal",
    "TV Size",
)
# No ASCII letter directly before or after, so compilation names like
# "LoveLive! …" (and "Alive") stay eligible while "LIVE2019" still hits. Words of
# a two-word keyword may be joined by spaces, hyphens or nothing ("TV-Size").
_KEYWORD_RES = tuple(
    (
        kw,
        re.compile(
            r"(?<![A-Za-z])" + r"[\s-]*".join(map(re.escape, kw.split())) + r"(?![A-Za-z])",
            re.IGNORECASE,
        ),
    )
    for kw in VERSION_KEYWORDS
)
_WS_RE = re.compile(r"\s+")


def title_key(title: str) -> str:
    """Exact-equality key: ``normalize_for_match`` with whitespace runs collapsed."""
    return _WS_RE.sub(" ", normalize_for_match(title))


def version_keyword(album: str) -> str | None:
    """The first ``VERSION_KEYWORDS`` entry found in ``album`` (after NFKC, so
    fullwidth letters hit too), else ``None``."""
    album = unicodedata.normalize("NFKC", album)
    for kw, pattern in _KEYWORD_RES:
        if pattern.search(album):
            return kw
    return None


def _ids(song: dict[str, Any]) -> list[str]:
    return [a["id"] for a in song.get("artists") or [] if a.get("id")]


def artist_ids(liked: Sequence[dict[str, Any]]) -> list[str]:
    """Distinct artist ids of the liked songs, first-seen order."""
    return list(dict.fromkeys(i for s in liked for i in _ids(s)))


@dataclass(frozen=True)
class Match:
    """One B: a listed track matching A, and the artist whose list holds it."""

    song: dict[str, Any]
    artist_id: str
    in_top: bool  # in Top songs of any of A's artists
    liked: bool  # B's videoId is in Liked songs


@dataclass(frozen=True)
class Candidate:
    """A liked track missing from its artists' lists, with its B's."""

    a: dict[str, Any]
    matches: tuple[Match, ...]
    shown: int  # times Liked songs shows A
    keyword: str | None  # version keyword in A's album

    @property
    def ambiguous(self) -> bool:
        return len(self.matches) > 1

    @property
    def report_only_cause(self) -> str | None:
        causes = []
        if self.keyword is not None:
            causes.append(f"version keyword '{self.keyword}' in A's album")
        if self.shown > 1:
            causes.append(f"A shown {self.shown} times in Liked songs")
        return "; ".join(causes) or None


@dataclass(frozen=True)
class DiscoverySummary:
    songs: int  # distinct liked videoIds
    listed: int  # eligible A's already in an artist's list
    candidates: int  # exactly one B
    ambiguous: int  # two or more B's
    unreadable: int  # eligible A's with an artist that couldn't be read


def find_candidates(
    liked: Sequence[dict[str, Any]],
    artists: Mapping[str, ArtistSongs | None],
) -> tuple[list[Candidate], DiscoverySummary]:
    """Apply the §2 candidate rule to each distinct liked ``videoId``.

    ``artists[id]`` is ``None`` for an artist without a songs list; an id
    absent from the mapping couldn't be read, and blocks every A crediting it.
    """
    # Unplayable rows come back with videoId None: nothing to like or compare.
    playable = [s for s in liked if s.get("videoId")]
    shown = Counter(s["videoId"] for s in playable)
    first: dict[str, dict[str, Any]] = {}
    for s in playable:
        first.setdefault(s["videoId"], s)

    candidates: list[Candidate] = []
    listed = unreadable = 0
    for vid, a in first.items():
        a_artists = list(dict.fromkeys(_ids(a)))
        duration = a.get("duration_seconds")
        if a.get("videoType") != ATV or not a_artists or duration is None:
            continue
        if any(i not in artists for i in a_artists):
            unreadable += 1
            continue
        pages = [(i, artists[i]) for i in a_artists if artists[i] is not None]
        top = {t for _, p in pages for t in p.top_video_ids}
        if vid in top or any(s["videoId"] == vid for _, p in pages for s in p.songs):
            listed += 1
            continue

        key = title_key(a["title"])
        found: dict[str, Match] = {}
        for artist_id, p in pages:
            for b in p.songs:
                b_vid = b["videoId"]
                b_duration = b.get("duration_seconds")
                # Unplayable playlist rows come back with videoId None.
                if (
                    b_vid is None
                    or b_vid in found
                    or b.get("videoType") != ATV
                    or b_duration is None
                    or abs(b_duration - duration) > DURATION_TOLERANCE_S
                    or title_key(b["title"]) != key
                ):
                    continue
                found[b_vid] = Match(b, artist_id, in_top=b_vid in top, liked=b_vid in shown)
        if found:
            album = (a.get("album") or {}).get("name") or ""
            candidates.append(
                Candidate(a, tuple(found.values()), shown[vid], version_keyword(album))
            )

    ambiguous = sum(c.ambiguous for c in candidates)
    summary = DiscoverySummary(
        songs=len(first),
        listed=listed,
        candidates=len(candidates) - ambiguous,
        ambiguous=ambiguous,
        unreadable=unreadable,
    )
    return candidates, summary


def describe(video_id: str, song: dict[str, Any]) -> str:
    album = (song.get("album") or {}).get("name") or "none"
    duration = song.get("duration_seconds")
    return f"{video_id} {song.get('title')!r} (album {album!r}, {duration} s)"


def artist_name(artist_id: str, *songs: dict[str, Any]) -> str:
    """The name ``songs`` credit ``artist_id`` with, else the id itself."""
    for s in songs:
        for artist in s.get("artists") or []:
            if artist.get("id") == artist_id and artist.get("name"):
                return str(artist["name"])
    return artist_id


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _reason(c: Candidate) -> str:
    a_vid = c.a["videoId"]
    parts = [f"A={describe(a_vid, c.a)}"]
    for m in c.matches:
        parts.append(
            f"B={describe(m.song['videoId'], m.song)}; "
            f"artist {artist_name(m.artist_id, c.a, m.song)!r} lists B; "
            f"B in Top songs: {_yes_no(m.in_top)}; B already liked: {_yes_no(m.liked)}"
        )
    if c.report_only_cause:
        parts.append(f"report-only: {c.report_only_cause}")
    return "; ".join(parts)


def persist_report(
    session: Session, snapshot_id: int, candidates: Sequence[Candidate]
) -> tuple[Diagnosis, int]:
    """Write the candidates as a ``canonicalize`` diagnosis on ``snapshot_id``.

    ``snapshot_id`` is the Liked-songs snapshot the candidates came from (made by
    the caller, so it exists before the slow artist reads). Returns the
    diagnosis and how many earlier ``'skipped'`` findings were carried over.
    Carry-over keys on issue type, so alignment rows never match these.
    """
    a_tracks = {
        vid: track_id
        for vid, track_id in session.execute(
            select(SnapshotItem.video_id, SnapshotItem.track_id)
            .where(SnapshotItem.snapshot_id == snapshot_id)
            .order_by(SnapshotItem.position.desc())
        )
    }
    diagnosis = Diagnosis(
        ytmusic_snapshot_id=snapshot_id,
        youtube_snapshot_id=None,
        kind=DIAGNOSIS_KIND_CANONICALIZE,
    )
    session.add(diagnosis)
    session.flush()
    for c in candidates:
        if c.ambiguous:
            issue_type, related = ISSUE_CANONICAL_AMBIGUOUS, None
        else:
            issue_type = ISSUE_CANONICAL_VERSION
            related = upsert_ytmusic_track(session, c.matches[0].song).id
        session.add(
            DiagnosisItem(
                diagnosis_id=diagnosis.id,
                issue_type=issue_type,
                confidence=0.5 if c.ambiguous or c.report_only_cause else 1.0,
                reason=_reason(c),
                source_track_id=a_tracks[c.a["videoId"]],
                related_track_id=related,
                status="open",
            )
        )
    session.flush()
    return diagnosis, carry_over_skipped(session, diagnosis)


def flagged_keywords(lm: Sequence[SnapshotItem]) -> dict[int, str]:
    """Track id -> version keyword in its album, for each track the Liked songs
    snapshot ``lm`` shows exactly once.

    A ``canonical_version`` at confidence 0.5 whose A is in here is report-only
    for the keyword alone: the other cause, A shown more than once, is ruled
    out by the same snapshot the report was built on (spec §6, rule 2).
    """
    shown = Counter(it.video_id for it in lm if it.video_id)
    return {
        it.track_id: kw
        for it in lm
        if it.video_id and shown[it.video_id] == 1 and (kw := version_keyword(it.album or ""))
    }


def plan(
    items: Sequence[DiagnosisItem],
    video_ids: Mapping[int, str],
    *,
    lm_position: Mapping[int, int],
    liked_video_ids: set[str],
    flagged: Mapping[int, str] | None = None,
) -> list[PlannedAction]:
    """Open ``canonical_version`` findings at confidence 1.0 become actions,
    and with ``flagged`` (``--include-flagged``: ``flagged_keywords`` of the
    report's LM) so do those whose A is in it.

    Pure. ``lm_position`` maps A's track id to its Liked songs position (0 =
    newest) and ``liked_video_ids`` is every videoId in that report's LM. The
    oldest goes first (highest position): each repoint moves B to the top, so
    the moved songs keep their relative order. Missing positions go last.

    The kind is a forecast; the dispatcher decides from B's live rating. B
    counts as liked once an earlier action of this plan likes it, so two A's
    sharing one B forecast a repoint, then a merge.
    """
    flagged = flagged or {}
    todo = [
        it
        for it in items
        if it.issue_type == ISSUE_CANONICAL_VERSION
        and it.status == "open"
        and (it.confidence == 1.0 or it.source_track_id in flagged)
    ]
    todo.sort(key=lambda it: (-lm_position.get(it.source_track_id, -1), it.id))
    liked = set(liked_video_ids)
    actions: list[PlannedAction] = []
    for it in todo:
        a, b = video_ids[it.source_track_id], video_ids[it.related_track_id]
        kind = "canonical_merge" if b in liked else "canonical_repoint"
        liked.add(b)
        actions.append(PlannedAction(it.id, kind, a_video_id=a, b_video_id=b))
    return actions


def summarize(actions: Sequence[PlannedAction]) -> str:
    """Human-readable plan summary. Plain text, like ``sync.summarize``."""
    by_kind = Counter(a.kind for a in actions)
    quota = sum(QUOTA_COST[a.kind] for a in actions)
    lines = [
        "Canonicalize plan:",
        f"  canonical_repoint: {by_kind['canonical_repoint']}",
        f"  canonical_merge: {by_kind['canonical_merge']}",
    ]
    if by_kind["canonical_repoint"]:
        lines.append("  note: each repoint moves B to the top of Liked videos and Liked songs")
    if by_kind["canonical_merge"]:
        lines.append("  note: each merge removes one entry from Liked songs")
    lines += quota_lines(quota)
    return "\n".join(lines)
