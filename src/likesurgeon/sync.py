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
    re-evaluates it. Exception: ``ytm_dedupe`` is non-idempotent and
    flips to ``"applied"`` after any attempt (success or failure) to
    prevent auto-retry over-removal.
  * Per-action commit cadence: a crash mid-run preserves prior actions'
    SyncAttempt rows AND any status updates already committed. Drift's
    two HTTP calls count as one action (one commit).
"""

from __future__ import annotations

import re
import time
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
from .snapshot import YTMUSIC_LIKED_SONGS
from .youtube_client import YouTubeClient, YouTubeWriteError
from .ytmusic_client import (
    AuthFileMissingError,
    UnexpectedResponseError,
    YTMusicClient,
    YTMusicWriteError,
)

ActionKind = Literal["yt_unlike", "ytm_like", "yt_relike", "ytm_dedupe"]
_DispatchOutcome = Literal["applied", "skipped", "failed"]

_YTM_LIKE_VERIFY_WAIT_SECONDS = 5

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


@dataclass(frozen=True)
class _DuplicateReason:
    count: int
    source: str
    positions: tuple[int, ...]


_DUPLICATE_REASON_RE = re.compile(
    r"^appears (?P<count>\d+) times in (?P<source>\w+) snapshot "
    r"\(positions: (?P<positions>[\d, ]+)\)$"
)


def _parse_duplicate_in_source_reason(reason: str) -> _DuplicateReason | None:
    """Parse the diagnosis-time reason emitted by ``build_duplicate_in_source_items``.

    Returns ``None`` for any deviation: malformed input, multi-source
    contamination, count-vs-positions mismatch (e.g.
    ``"appears 2 times ... (positions: 0, 1, 5)"``). Callers MUST treat
    ``None`` as "do not auto-act."
    """
    m = _DUPLICATE_REASON_RE.fullmatch(reason)
    if m is None:
        return None
    count = int(m.group("count"))
    try:
        positions = tuple(int(p.strip()) for p in m.group("positions").split(","))
    except ValueError:
        return None
    if len(positions) != count:
        return None
    return _DuplicateReason(count=count, source=m.group("source"), positions=positions)


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
    Findings of type ``ytmusic_only`` or ``metadata_drift`` are silently
    dropped (informational, not actionable in 0.5).
    ``duplicate_in_source`` maps to ``ytm_dedupe`` only when the parsed
    reason validates as ytmusic-source + N=2 + matching position count.
    All other shapes produce a ``SkipRecord`` — we never default to a
    destructive YT Music write.
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

        elif item.issue_type in {ISSUE_YTMUSIC_ONLY, ISSUE_METADATA_DRIFT}:
            # Informational findings — no record, no action.
            continue

        elif item.issue_type == ISSUE_DUPLICATE_IN_SOURCE:
            parsed = _parse_duplicate_in_source_reason(item.reason)
            if parsed is None:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="ytm_dedupe",
                        reason="unparseable duplicate_in_source reason",
                    )
                )
                continue
            if parsed.source != YTMUSIC_LIKED_SONGS:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="ytm_dedupe",
                        reason=f"duplicate in {parsed.source} source; not handled in 0.5",
                    )
                )
                continue
            if parsed.count != 2:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="ytm_dedupe",
                        reason=(
                            f"count={parsed.count}; only N=2 is auto-handled in 0.5. "
                            "File an issue for higher counts."
                        ),
                    )
                )
                continue
            primary = _video_id_for(video_ids, item.source_track_id)
            if not primary:
                skips.append(
                    SkipRecord(
                        item_id=item.id,
                        kind="ytm_dedupe",
                        reason="no video_id available for source track",
                    )
                )
                continue
            actions.append(
                PlannedAction(
                    item_id=item.id,
                    kind="ytm_dedupe",
                    primary_video_id=primary,
                    secondary_video_id=None,
                )
            )

        # Unknown future issue types fall through silently (no WARN channel
        # — sync.plan stays pure).

    return actions, skips


def _video_id_for(video_ids: dict[int, str], track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return video_ids.get(track_id)


_PLAN_ACTION_KINDS = ("yt_unlike", "ytm_like", "yt_relike", "ytm_dedupe")

# Quota cost per action kind (YouTube `videos.rate` = 50 units; ytm is free).
# Drift's worst case = 100 (like 50 + unlike 50 if the like succeeds).
_QUOTA_COST: dict[ActionKind, int] = {
    "yt_unlike": 50,
    # rate-none + rate-like (verify reads are free; client retries are transparent)
    "ytm_like": 100,
    "yt_relike": 100,
    "ytm_dedupe": 0,
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
    for kind in _PLAN_ACTION_KINDS:
        lines.append(f"  {kind}: {by_action.get(kind, 0)}")
    lines.append(f"  skipped: {len(skips)}")
    if by_skip:
        for kind in _PLAN_ACTION_KINDS:
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

    Carve-out: ``ytm_dedupe`` flips ``item.status`` to ``"applied"``
    regardless of success. The call is non-idempotent — auto-retrying
    a failed unlike risks over-removal because client failure can't
    distinguish "server processed, client errored" from "server didn't
    process". Genuine failures self-correct via the next compare-likes.
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

        outcome = _dispatch(action, ytm=ytm, yt=yt, session=session)
        if outcome == "applied":
            applied += 1
            item.status = "applied"
        elif outcome == "skipped":
            skipped += 1
            item.status = "skipped"
        else:
            failed += 1
        # ytm_dedupe is non-idempotent — terminal-on-attempt regardless of success.
        # Client failure doesn't distinguish "server processed, client errored"
        # from "server didn't process", so auto-retry would risk over-removal.
        # Genuine failures self-correct via the next compare-likes (lingering dup
        # → new finding → new attempt).
        if outcome == "failed" and action.kind == "ytm_dedupe":
            item.status = "applied"
        session.commit()

    return ExecResult(applied=applied, failed=failed, skipped=skipped)


def _dispatch(
    action: PlannedAction,
    *,
    ytm: YTMusicClient,
    yt: YouTubeClient,
    session: Session,
) -> _DispatchOutcome:
    """Run ``action``'s HTTP call(s), record SyncAttempt rows, return outcome.

    ``"applied"`` iff every call for the action succeeded — i.e. the
    dispatcher is allowed to flip ``DiagnosisItem.status`` to ``'applied'``.
    ``"skipped"`` is a terminal non-failure outcome (currently only
    ``ytm_like`` post-verify when cross-prop didn't observe the song);
    ``"failed"`` is anything else.

    Note: for ``ytm_dedupe``, the outcome is used only for ``ExecResult``
    counting. Terminality (``status='applied'`` regardless of success) is
    handled by ``execute()``, not here.
    """
    if action.kind == "yt_unlike":
        ok = _try_yt_rate(
            session,
            action.item_id,
            kind="yt_unlike",
            yt=yt,
            video_id=action.primary_video_id,
            rating="none",
        )
        return "applied" if ok else "failed"

    if action.kind == "ytm_like":
        return _try_ytm_like(
            session,
            action.item_id,
            ytm=ytm,
            yt=yt,
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
            return "failed"
        # Defensive — planner always provides secondary for yt_relike,
        # but the type signature permits None.
        if action.secondary_video_id is None:
            return "applied"
        ok = _try_yt_rate(
            session,
            action.item_id,
            kind="yt_relike_unlike",
            yt=yt,
            video_id=action.secondary_video_id,
            rating="none",
        )
        return "applied" if ok else "failed"

    if action.kind == "ytm_dedupe":
        ok = _try_ytm_unlike(
            session,
            action.item_id,
            ytm=ytm,
            video_id=action.primary_video_id,
        )
        return "applied" if ok else "failed"

    # Unreachable for the closed ActionKind set, but keeps the function
    # total for static analyzers.
    return "failed"


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
    yt: YouTubeClient,
    video_id: str,
) -> _DispatchOutcome:
    """Cross-prop like: YouTube unlike → relike → wait → verify on YT Music.

    YT Music has no first-class API to like a song from outside the
    YouTube property. The reliable observed path is to toggle the YouTube
    like off then back on, which YouTube propagates to the YT Music
    "Liked songs" playlist within a few seconds. We then re-fetch LM and
    check for the ``video_id`` to confirm.

    Outcomes (each step records a SyncAttempt row with a distinct kind):
      * ``"applied"`` — both YouTube calls + verify all succeeded and
        the song is now in LM.
      * ``"skipped"`` — both YouTube calls succeeded but the song wasn't
        observed in LM within the verify window. Cross-prop didn't fire
        (e.g. song is YouTube-only). Terminal: retrying just repeats the
        same outcome.
      * ``"failed"`` — any HTTP/auth failure. ``DiagnosisItem.status``
        stays ``"open"`` so the next run retries. Note: if the unlike
        succeeded but the relike failed, the video is left unliked on
        YouTube; manual relike may be needed (rare).
    """
    # Step 1: YouTube unlike
    try:
        yt.rate_video(video_id, "none")
    except YouTubeWriteError as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_like_yt_unlike",
                status="failed",
                reason=str(exc),
            )
        )
        return "failed"
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind="ytm_like_yt_unlike",
            status="applied",
            reason="rate(none) ok",
        )
    )

    # Step 2: YouTube relike
    try:
        yt.rate_video(video_id, "like")
    except YouTubeWriteError as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_like_yt_relike",
                status="failed",
                reason=str(exc),
            )
        )
        # vid left unliked on YouTube; rare, manual relike if needed
        return "failed"
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind="ytm_like_yt_relike",
            status="applied",
            reason="rate(like) ok",
        )
    )

    # Step 3: cross-prop wait + verify
    time.sleep(_YTM_LIKE_VERIFY_WAIT_SECONDS)
    try:
        present = ytm.is_in_liked_songs(video_id)
    except (UnexpectedResponseError, AuthFileMissingError) as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_like_verify",
                status="failed",
                reason=str(exc),
            )
        )
        return "failed"
    if present:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_like_verify",
                status="applied",
                reason="present in ytmusic library",
            )
        )
        return "applied"
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind="ytm_like_verify",
            status="skipped",
            reason="not observed within first 10000 liked songs after +5s",
        )
    )
    return "skipped"


def _try_ytm_unlike(
    session: Session,
    item_id: int,
    *,
    ytm: YTMusicClient,
    video_id: str,
) -> bool:
    try:
        ytm.unlike_song(video_id)
    except YTMusicWriteError as exc:
        session.add(
            SyncAttempt(
                diagnosis_item_id=item_id,
                kind="ytm_dedupe",
                status="failed",
                reason=str(exc),
            )
        )
        return False
    session.add(
        SyncAttempt(
            diagnosis_item_id=item_id,
            kind="ytm_dedupe",
            status="applied",
            reason="ok",
        )
    )
    return True
