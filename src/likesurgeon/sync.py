"""Planner + dispatcher for the ``sync`` command (write actions).

Pure planner (`plan`, `summarize`) is testable without any clients; the
dispatcher (`execute`) takes mock-friendly client objects and is the only
thing that mutates ``DiagnosisItem.status`` and emits ``SyncAttempt`` rows.

Invariants:
  * ``DiagnosisItem.reason`` is never modified — that field is the
    diagnosis-time evidence and must outlive sync attempts. Sync-side
    detail (errors, threshold notes, missing video_ids) lives on
    ``SyncAttempt.reason`` instead.
  * ``DiagnosisItem.status`` flips to ``"applied"`` only when every API
    call for the action succeeded (drift = both halves). Anything else
    (failure, skip) leaves it as ``"open"`` so the next ``sync`` run
    re-evaluates it.
  * Per-action commit cadence: a crash mid-run preserves prior actions'
    SyncAttempt rows AND any status updates already committed. Drift's
    two HTTP calls count as one action (one commit).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .diagnosis import (
    ISSUE_DUPLICATE_IN_SOURCE,
    ISSUE_METADATA_DRIFT,
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_UNAVAILABLE_VIDEO,
    ISSUE_YTMUSIC_ONLY,
)
from .models import DiagnosisItem, SyncAttempt, Track
from .youtube_client import YouTubeClient, YouTubeWriteError
from .ytmusic_client import YTMusicClient, YTMusicWriteError

ActionKind = Literal["yt_unlike", "ytm_like", "yt_relike"]

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
    item_id: int
    kind: ActionKind
    primary_video_id: str
    secondary_video_id: str | None  # only populated for ``yt_relike``


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


@dataclass(frozen=True)
class ExecResult:
    applied: int
    failed: int
    skipped: int


def plan(
    items: list[DiagnosisItem],
    video_ids: dict[int, str],
    *,
    drift_min_confidence: float,
) -> tuple[list[PlannedAction], list[SkipRecord]]:
    """Map findings to actions / skips. Pure — no I/O, no client calls.

    Items with ``status`` in {``'applied'``, ``'skipped'``} are silently
    dropped (terminal at item level — re-running sync must not re-attempt
    them, and there's nothing to record). ``'applied'`` is set by ``execute``
    when every API call for an action succeeded; ``'skipped'`` is reserved
    for an explicit manual override ("I never want to act on this finding"
    — e.g. a private/deleted YouTube ghost that ``videos.rate`` can't
    unlike anyway, so retrying would just noise the audit log forever).
    Findings of type ``ytmusic_only``, ``metadata_drift``, or
    ``duplicate_in_source`` are also silently dropped (informational, not
    actionable in 0.4 — within-source dedup is deferred to 0.5).
    """
    actions: list[PlannedAction] = []
    skips: list[SkipRecord] = []

    for item in items:
        if item.status in ("applied", "skipped"):
            continue

        if item.issue_type == ISSUE_UNAVAILABLE_VIDEO:
            primary = _video_id_for(video_ids, item.source_track_id)
            if primary is None:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="yt_unlike",
                        reason="no video_id available for source track",
                    )
                )
                continue
            actions.append(
                PlannedAction(
                    item_id=item.id,
                    kind="yt_unlike",
                    primary_video_id=primary,
                    secondary_video_id=None,
                )
            )

        elif item.issue_type == ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC:
            primary = _video_id_for(video_ids, item.source_track_id)
            if primary is None:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="ytm_like",
                        reason="no video_id available for source track",
                    )
                )
                continue
            actions.append(
                PlannedAction(
                    item_id=item.id,
                    kind="ytm_like",
                    primary_video_id=primary,
                    secondary_video_id=None,
                )
            )

        elif item.issue_type == ISSUE_POINTER_DRIFT:
            if item.confidence < drift_min_confidence:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="yt_relike",
                        reason=f"confidence {item.confidence} < {drift_min_confidence}",
                    )
                )
                continue
            primary = _video_id_for(video_ids, item.related_track_id)
            secondary = _video_id_for(video_ids, item.source_track_id)
            if primary is None or secondary is None:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="yt_relike",
                        reason="no video_id available for source/related track",
                    )
                )
                continue
            actions.append(
                PlannedAction(
                    item_id=item.id,
                    kind="yt_relike",
                    primary_video_id=primary,
                    secondary_video_id=secondary,
                )
            )

        elif item.issue_type in {
            ISSUE_YTMUSIC_ONLY,
            ISSUE_METADATA_DRIFT,
            ISSUE_DUPLICATE_IN_SOURCE,
        }:
            # Informational findings — no record, no action.
            continue

        # Unknown future issue types fall through silently (no WARN channel
        # — sync.plan stays pure).

    return actions, skips


def _video_id_for(video_ids: dict[int, str], track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return video_ids.get(track_id)


# Quota cost per action kind (YouTube `videos.rate` = 50 units; ytm is free).
# Drift's worst case = 100 (like 50 + unlike 50 if the like succeeds).
_QUOTA_COST: dict[ActionKind, int] = {
    "yt_unlike": 50,
    "ytm_like": 0,
    "yt_relike": 100,
}


def summarize(actions: list[PlannedAction], skips: list[SkipRecord]) -> str:
    """Human-readable plan summary. Plain text — Rich rendering belongs in cli.py."""
    by_action: dict[str, int] = {}
    for a in actions:
        by_action[a.kind] = by_action.get(a.kind, 0) + 1
    by_skip: dict[str, int] = {}
    for s in skips:
        by_skip[s.kind] = by_skip.get(s.kind, 0) + 1

    quota = sum(_QUOTA_COST[a.kind] for a in actions)

    lines = ["Sync plan:"]
    for kind in ("yt_unlike", "ytm_like", "yt_relike"):
        lines.append(f"  {kind}: {by_action.get(kind, 0)}")
    lines.append(f"  skipped: {len(skips)}")
    if by_skip:
        for kind in ("yt_unlike", "ytm_like", "yt_relike"):
            count = by_skip.get(kind, 0)
            if count:
                lines.append(f"    {kind}: {count}")
    lines.append(f"Estimated YouTube quota: {quota} units (daily default 10000)")
    return "\n".join(lines)


def execute(
    session: Session,
    actions: list[PlannedAction],
    skips: list[SkipRecord],
    *,
    ytm: YTMusicClient,
    yt: YouTubeClient,
) -> ExecResult:
    """Apply ``actions``, record one SyncAttempt per HTTP call (and per skip).

    ``ExecResult`` counts FINDINGS (one tally per action / skip), not HTTP
    calls — drift's two halves are one action. A drift where the like
    succeeded but the unlike failed counts as ``failed`` (the item stays
    ``open``). The per-call detail lives in the ``SyncAttempt`` rows.

    Commit cadence: per-action. A crash mid-run preserves all prior
    commits — the next ``sync`` re-evaluates anything not at
    ``status='applied'``.
    """
    applied = 0
    failed = 0
    skipped = 0

    for skip in skips:
        session.add(
            SyncAttempt(
                diagnosis_item_id=skip.item_id,
                kind=skip.kind,
                status="skipped",
                reason=skip.reason,
            )
        )
        skipped += 1
        session.commit()

    for action in actions:
        item = session.get(DiagnosisItem, action.item_id)
        # ``item`` is non-None in normal flow — the planner only emits
        # actions for items it just iterated; defensive None-skip just
        # in case a concurrent delete raced us.
        if item is None:
            continue

        ok = _dispatch(action, ytm=ytm, yt=yt, session=session)
        if ok:
            item.status = "applied"
            applied += 1
        else:
            failed += 1
        session.commit()

    return ExecResult(applied=applied, failed=failed, skipped=skipped)


def _dispatch(
    action: PlannedAction,
    *,
    ytm: YTMusicClient,
    yt: YouTubeClient,
    session: Session,
) -> bool:
    """Run ``action``'s HTTP call(s), record SyncAttempt rows, return success.

    ``True`` iff every call for the action succeeded — i.e. the dispatcher
    is allowed to flip ``DiagnosisItem.status`` to ``'applied'``.
    """
    if action.kind == "yt_unlike":
        return _try_yt_rate(
            session,
            action.item_id,
            kind="yt_unlike",
            yt=yt,
            video_id=action.primary_video_id,
            rating="none",
        )

    if action.kind == "ytm_like":
        return _try_ytm_like(
            session,
            action.item_id,
            ytm=ytm,
            video_id=action.primary_video_id,
        )

    if action.kind == "yt_relike":
        # like first, unlike only on like-success — see plan Decision 5
        # ("safe fail": unlike failure leaves a duplicate like the next
        # run can clean up; like failure leaves the original like alone).
        like_ok = _try_yt_rate(
            session,
            action.item_id,
            kind="yt_relike_like",
            yt=yt,
            video_id=action.primary_video_id,
            rating="like",
        )
        if not like_ok:
            return False
        # Defensive — planner always provides secondary for yt_relike,
        # but the type signature permits None.
        if action.secondary_video_id is None:
            return True
        return _try_yt_rate(
            session,
            action.item_id,
            kind="yt_relike_unlike",
            yt=yt,
            video_id=action.secondary_video_id,
            rating="none",
        )

    # Unreachable for the closed ActionKind set, but keeps the function
    # total for static analyzers.
    return False


def _try_yt_rate(
    session: Session,
    item_id: int,
    *,
    kind: str,
    yt: YouTubeClient,
    video_id: str,
    rating: Literal["like", "none"],
) -> bool:
    try:
        yt.rate_video(video_id, rating)
    except YouTubeWriteError as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind=kind,
                status="failed",
                reason=str(exc),
            )
        )
        return False
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind=kind,
            status="applied",
            reason="ok",
        )
    )
    return True


def _try_ytm_like(
    session: Session,
    item_id: int,
    *,
    ytm: YTMusicClient,
    video_id: str,
) -> bool:
    try:
        ytm.like_song(video_id)
    except YTMusicWriteError as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_like",
                status="failed",
                reason=str(exc),
            )
        )
        return False
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind="ytm_like",
            status="applied",
            reason="ok",
        )
    )
    return True
