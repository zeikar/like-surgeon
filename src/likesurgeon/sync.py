"""Planner for the ``sync`` command (spec §4.1): which findings become writes.

``plan`` and ``summarize`` are pure; ``resolve_video_ids`` looks up the video
ids ``plan`` needs. A write-eligible pair finding (A = the YouTube video whose
like backs an LM entry, B = the ``videoId`` that entry shows) becomes one of
two actions, which ``sync_dispatch.execute`` runs:

  * ``repoint``: like B, confirm, unlike A, confirm.
  * ``unlike_shadow`` (B is already liked): confirm B is liked, unlike A,
    confirm.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .diagnosis import (
    ISSUE_RENDERED_AS_OTHER,
    ISSUE_SHADOW_DUPLICATE,
    PAIR_ISSUE_TYPES,
    is_write_eligible,
)
from .models import DiagnosisItem, Track

ActionKind = Literal["repoint", "unlike_shadow"]

_TRACK_LOOKUP_BATCH_SIZE = 500


def _video_ids_for_tracks(session: Session, track_ids: set[int]) -> dict[int, str | None]:
    """Map ``track_ids`` to their ``Track.video_id`` values.

    Issues the lookup in batches of ``_TRACK_LOOKUP_BATCH_SIZE`` so the
    IN(...) clause never exceeds SQLite's ``SQLITE_MAX_VARIABLE_NUMBER``
    (which can be as low as 999 on older builds). The default 500 keeps
    each query well under that ceiling on every supported sqlite.
    """
    if not track_ids:
        return {}
    out: dict[int, str | None] = {}
    ids = list(track_ids)
    for start in range(0, len(ids), _TRACK_LOOKUP_BATCH_SIZE):
        chunk = ids[start : start + _TRACK_LOOKUP_BATCH_SIZE]
        rows = session.scalars(select(Track).where(Track.id.in_(chunk))).all()
        for t in rows:
            out[t.id] = t.video_id
    return out


def resolve_video_ids(session: Session, items: list[DiagnosisItem]) -> dict[int, str]:
    """Build ``{track_id: video_id}`` for every track referenced by ``items``.

    Tracks whose ``video_id`` is NULL or empty are excluded — they have
    no addressable target for write actions, so the planner emits a
    ``SkipRecord`` for any item that needed them.
    """
    track_ids: set[int] = set()
    for it in items:
        if it.source_track_id is not None:
            track_ids.add(it.source_track_id)
        if it.related_track_id is not None:
            track_ids.add(it.related_track_id)
    raw = _video_ids_for_tracks(session, track_ids)
    return {tid: vid for tid, vid in raw.items() if vid}


@dataclass(frozen=True)
class PlannedAction:
    """A = the YouTube video to unlike; B = the YT Music track to keep —
    liked first by ``repoint``, confirmed already liked by ``unlike_shadow``."""

    item_id: int
    kind: ActionKind
    a_video_id: str
    b_video_id: str


@dataclass(frozen=True)
class SkipRecord:
    """A finding the planner refused to act on.

    ``kind`` records *what* would have been attempted (so the audit row
    is comparable to the action rows); ``reason`` records *why* it was
    skipped.
    """

    item_id: int
    kind: ActionKind
    reason: str


def plan(
    items: list[DiagnosisItem],
    video_ids: dict[int, str],
    availability: dict[int, bool | None],
    *,
    include_playable: bool = False,
) -> tuple[list[PlannedAction], list[SkipRecord]]:
    """Map pair findings to actions / skips. Pure — no I/O, no client calls.

    ``availability`` maps an LL track id (A) to ``is_available`` from the
    diagnosis's YouTube scan. Items at ``'applied'`` / ``'skipped'`` and
    findings other than ``PAIR_ISSUE_TYPES`` are dropped silently.

      * ``relinked`` → ``repoint``.
      * ``rendered_as_other`` (A playable) → ``repoint`` only with
        ``include_playable``.
      * ``shadow_duplicate`` → ``unlike_shadow`` when A was unavailable, or
        playable with ``include_playable``; unknown availability never.

    Acting on a playable A removes a like the user made, hence the opt-in.
    Pairs that aren't write-eligible, are opted out, or lack a video id
    become ``SkipRecord``s.
    """
    actions: list[PlannedAction] = []
    skips: list[SkipRecord] = []

    for item in items:
        if item.status in ("applied", "skipped") or item.issue_type not in PAIR_ISSUE_TYPES:
            continue
        kind: ActionKind = (
            "unlike_shadow" if item.issue_type == ISSUE_SHADOW_DUPLICATE else "repoint"
        )
        a = _video_id_for(video_ids, item.source_track_id)
        b = _video_id_for(video_ids, item.related_track_id)
        reason = _skip_reason(item, availability, include_playable=include_playable)
        if reason is not None:
            skips.append(SkipRecord(item_id=item.id, kind=kind, reason=reason))
        elif a is None or b is None:
            skips.append(
                SkipRecord(
                    item_id=item.id,
                    kind=kind,
                    reason="no video_id for A (YouTube video) or B (YT Music entry)",
                )
            )
        else:
            actions.append(PlannedAction(item_id=item.id, kind=kind, a_video_id=a, b_video_id=b))

    return actions, skips


def _skip_reason(
    item: DiagnosisItem, availability: dict[int, bool | None], *, include_playable: bool
) -> str | None:
    if not is_write_eligible(item):
        return "report-only finding (pair unconfirmed or A's availability unknown; see `issues`)"
    if item.issue_type == ISSUE_SHADOW_DUPLICATE:
        playable = availability.get(item.source_track_id) if item.source_track_id else None
        if playable is None:
            return "A's availability is unknown in the YouTube scan"
    else:
        playable = item.issue_type == ISSUE_RENDERED_AS_OTHER
    if playable and not include_playable:
        return "A is a playable video you liked; pass --include-playable to act on it"
    return None


def _video_id_for(video_ids: dict[int, str], track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return video_ids.get(track_id)


_ACTION_KINDS: tuple[ActionKind, ...] = ("repoint", "unlike_shadow")

# YouTube quota: ``videos.rate`` = 50 units, ``videos.getRating`` = 1. A
# restore (re-like + getRating = 51) only runs after a failed LM check, so it
# isn't in the estimate. YT Music reads are free.
_DAILY_QUOTA = 10000

_QUOTA_COST: dict[ActionKind, int] = {
    # getRating A + getRating B + like B + getRating B + unlike A + getRating A
    "repoint": 104,
    # getRating B + getRating A + unlike A + getRating A
    "unlike_shadow": 53,
}


def summarize(actions: list[PlannedAction], skips: list[SkipRecord]) -> str:
    """Human-readable plan summary. Plain text — Rich rendering belongs in cli.py."""
    by_action = Counter(a.kind for a in actions)
    by_skip = Counter(s.kind for s in skips)
    quota = sum(_QUOTA_COST[a.kind] for a in actions)

    lines = ["Sync plan:"]
    lines += [f"  {kind}: {by_action[kind]}" for kind in _ACTION_KINDS]
    lines.append(f"  skipped: {len(skips)}")
    lines += [f"    {kind}: {by_skip[kind]}" for kind in _ACTION_KINDS if by_skip[kind]]
    lines.append(f"Estimated YouTube quota: {quota} units (daily default {_DAILY_QUOTA})")
    if quota > _DAILY_QUOTA:
        lines.append(
            "  WARNING: exceeds the default daily quota — the run stops when it runs "
            "out; slice it with --limit"
        )
    return "\n".join(lines)
