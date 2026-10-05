"""Dispatcher for the ``sync`` command: runs the actions ``sync.plan`` made.

``execute`` takes mock-friendly client objects and is the only thing that
mutates ``DiagnosisItem.status`` and emits ``SyncAttempt`` rows.

YT Music Liked songs (LM) renders YouTube likes, so unliking A removes
whatever LM entry A backs — with a wrong pair, an unrelated song. Each unlike
that was sent is therefore followed by one LM-wide check (repoint: LM
unchanged; unlike_shadow: one B fewer) and, if LM doesn't look as expected, a
re-like of A. An A that still reads liked after its unlike is re-liked before
the check, so an unlike landing late can't win unnoticed. An A whose re-like
can't be confirmed is reported in ``ExecResult.left_unliked``.

Each action first reads A's rating (A must still be liked — the user may have
unliked it since the scan) and, for a repoint, B's (B must not be liked yet);
a stale finding is skipped without writes. A repoint that leaves A liked after
B's like may have landed (A restored, A's unlike rejected by the quota, B's
like erroring or unconfirmed) undoes B's like, so nothing is left behind; a B
left liked is reported in ``ExecResult.left_liked``. Any action that needs a
restore, successful or not, or whose B undo fails stops the run: a mismatch
means an order-derived pair was wrong, and later pairs of the same diagnosis
may be too.

Invariants:
  * ``DiagnosisItem.reason`` is never modified — that field is the
    diagnosis-time evidence. Sync-side detail lives on ``SyncAttempt.reason``.
  * One ``SyncAttempt`` per HTTP call, per LM check and per skip decision,
    plus an ``interrupted`` row when anything stops an action once A's
    unlike call has started.
  * ``DiagnosisItem.status`` flips to ``"applied"`` only when every call for
    the action succeeded and the LM check passed. ``"skipped"`` is terminal:
    B's like didn't land, a precheck didn't hold, or A's like was restored
    (the unlike didn't take, or the LM check failed) — a retry would repeat
    it (``likesurgeon unskip`` re-opens one). Anything else stays
    ``"open"``. A *planner-level* skip only writes an audit row; the item
    stays ``"open"``.
  * Commit cadence: per action, plus right after every write call, so a
    crash or kill at any later point still leaves that row on disk — the
    next ``sync`` refuses until a re-scan, and an A whose unlike was sent
    shows in ``stranded_unliked_video_ids``.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from .models import DiagnosisItem, SyncAttempt
from .sync import PlannedAction, SkipRecord
from .youtube_client import YouTubeClient, YouTubeQuotaExceededError, YouTubeWriteError
from .ytmusic_client import AuthFileMissingError, UnexpectedResponseError, YTMusicClient

_DispatchOutcome = Literal["applied", "skipped", "failed"]
_RateResult = Literal["ok", "error", "quota"]
_LM = Counter[str | None]

_VERIFY_WAIT_SECONDS = 5
_LM_RECHECK_WAIT_SECONDS = 15
_LM_DIFF_SHOWN = 6

# Starts the reason of a ``videos.rate`` row the daily quota rejected, i.e.
# nothing was sent. ``stranded_unliked_video_ids`` keys on it.
QUOTA_REJECTED = "not sent, YouTube quota exhausted"

# ``ExecResult.aborted`` when B's like couldn't be undone after a repoint.
ROLLBACK_STOP = (
    "a repoint's B like couldn't be undone, so the account differs from the diagnosis — "
    "unlike B by hand, then re-scan both sources and re-run compare-likes"
)

# ``ExecResult.aborted`` after an action needed a restore (whether or not it worked).
RESTORED_STOP = (
    "an action needed A re-liked, so the diagnosis' pairs can't be trusted — "
    "re-scan both sources and re-run compare-likes"
)


@dataclass(frozen=True)
class ExecResult:
    applied: int
    failed: int
    skipped: int
    # Of ``skipped``: actions undone by a confirmed re-like of A — the unlike
    # didn't take, or LM didn't change as expected.
    restored: int = 0
    # YouTube's daily quota ran out mid-run.
    quota_exhausted: bool = False
    # Actions left 'open' without any API call because the run stopped early
    # (quota, or ``aborted``).
    unattempted: int = 0
    # Videos unliked on YouTube whose restoring re-like failed or couldn't be
    # confirmed — possibly liked nowhere now; the user re-likes them by hand.
    left_unliked: tuple[str, ...] = ()
    # Why the run stopped early: YT Music liked songs couldn't be read, or an
    # action was restored (the diagnosis' pairs can't be trusted any more).
    aborted: str | None = None
    # Videos a repoint of this run liked on YouTube (B) that stayed liked or
    # may have: the undo failed or couldn't be confirmed (A restored, A's unlike
    # rejected, or B's like erroring). An unintended like, not a lost song;
    # the user unlikes them by hand.
    left_liked: tuple[str, ...] = ()


def execute(
    session: Session,
    actions: list[PlannedAction],
    skips: list[SkipRecord],
    *,
    ytm: YTMusicClient,
    yt: YouTubeClient,
) -> ExecResult:
    """Apply ``actions`` in order; each write's row is committed as it happens.

    ``ExecResult`` counts FINDINGS (one tally per action / skip), not HTTP
    calls; the per-call detail lives in the ``SyncAttempt`` rows.

    Each action is checked against the full LM read before it — the previous
    action's post-check read when that matched, a fresh read otherwise. An LM
    that can't be read stops the run (``aborted``): no action runs without a
    baseline to check against. YouTube quota exhaustion stops it too, but only
    once the action in flight has finished its LM check and any restore —
    ending it earlier could leave A unliked unnoticed. Other write errors are
    per-action (continue-on-error).
    """
    for skip in skips:
        session.add(
            SyncAttempt(
                diagnosis_item_id=skip.item_id,
                kind=skip.kind,
                status="skipped",
                reason=skip.reason,
            )
        )
        session.commit()

    run = _SyncRun(session, yt=yt, ytm=ytm)
    outcomes: Counter[_DispatchOutcome] = Counter()
    unattempted = 0
    for i, action in enumerate(actions):
        item = session.get(DiagnosisItem, action.item_id)
        # ``item`` is non-None in normal flow — the planner only emits
        # actions for items it just iterated; defensive None-skip just
        # in case a concurrent delete raced us.
        if item is None:
            continue
        before = run.take_lm_baseline()
        restored_before = (run.restored, len(run.left_unliked))
        if before is None:
            unattempted = len(actions) - i
            break
        if action.kind == "repoint":
            outcome = run.repoint(action, before)
        else:
            outcome = run.unlike_shadow(action, before)
        outcomes[outcome] += 1
        if (run.restored, len(run.left_unliked)) != restored_before and run.aborted is None:
            run.aborted = RESTORED_STOP
        if outcome in ("applied", "skipped"):
            item.status = outcome
        session.commit()

    return ExecResult(
        applied=outcomes["applied"],
        failed=outcomes["failed"],
        skipped=len(skips) + outcomes["skipped"],
        restored=run.restored,
        quota_exhausted=run.quota_exhausted,
        unattempted=unattempted,
        left_unliked=tuple(run.left_unliked),
        aborted=run.aborted,
        left_liked=tuple(run.left_liked),
    )


def _read_lm(ytm: YTMusicClient) -> _LM:
    """The whole LM as a multiset of ``videoId`` — the check compares counts, not order."""
    return Counter(t.get("videoId") for t in ytm.fetch_liked_songs() if isinstance(t, dict))


def _lm_diff(base: _LM, other: _LM) -> str:
    """How ``other`` differs from ``base``, e.g. ``-C +B`` (capped)."""
    parts = [f"-{v}" for v in (base - other).elements()]
    parts += [f"+{v}" for v in (other - base).elements()]
    if len(parts) > _LM_DIFF_SHOWN:
        parts[_LM_DIFF_SHOWN:] = [f"… {len(parts) - _LM_DIFF_SHOWN} more"]
    return " ".join(parts) or "no change"


def _unconfirmed(read: bool | None) -> _DispatchOutcome:
    """Outcome when B's like isn't confirmed: retried next run if the read
    failed (None), terminal if B reads not liked."""
    return "failed" if read is None else "skipped"


class _SyncRun:
    """State one ``execute`` shares across its actions."""

    def __init__(self, session: Session, *, yt: YouTubeClient, ytm: YTMusicClient) -> None:
        self.session = session
        self.yt = yt
        self.ytm = ytm
        # LM as last read and found as expected; None once a write may have
        # changed it since.
        self.lm: _LM | None = None
        self.quota_exhausted = False
        self.aborted: str | None = None
        self.restored = 0
        self.left_unliked: list[str] = []
        self.left_liked: list[str] = []
        # Set by ``_unlike_and_check``: the daily quota rejected A's unlike.
        self.unlike_rejected = False

    def take_lm_baseline(self) -> _LM | None:
        """The LM the next action is checked against, or None if the run stops.

        Taking it marks LM unknown until that action's own check matches.
        """
        if self.quota_exhausted or self.aborted is not None:
            return None
        before, self.lm = self.lm, None
        if before is None:
            try:
                before = _read_lm(self.ytm)
            except (UnexpectedResponseError, AuthFileMissingError) as exc:
                self.aborted = f"could not read YT Music liked songs before the next action: {exc}"
        return before

    def repoint(self, action: PlannedAction, before: _LM) -> _DispatchOutcome:
        item_id, a, b = action.item_id, action.a_video_id, action.b_video_id
        a_liked = self._has_rating(
            item_id, "repoint_precheck_a", a, "like", miss="skipped", wait=False
        )
        if not a_liked:
            return _unconfirmed(a_liked)
        # B already liked: the finding is stale (and a rollback couldn't tell
        # this run's like from the user's). Unread: don't write.
        b_liked = self._has_rating(
            item_id,
            "repoint_precheck_b",
            b,
            "none",
            miss="skipped",
            wait=False,
            miss_note="B is already liked — the finding is stale; re-scan both sources and "
            "re-run compare-likes",
        )
        if not b_liked:
            return _unconfirmed(b_liked)
        rated = self._rate(item_id, "repoint_like", b, "like")
        if rated == "quota":
            return "failed"  # nothing was sent
        # A 2xx doesn't mean B's like landed (and an error may have): unliking
        # A without it would drop the song from both platforms.
        liked = self._has_rating(item_id, "repoint_verify", b, "like", miss="skipped")
        if rated != "ok":
            # A is untouched; whatever landed of B's like is undone.
            if liked is not False:
                self._undo_b_like(item_id, b)
            return "failed"
        if not liked:
            if liked is None:
                self._undo_b_like(item_id, b)  # unread: B may be liked
            return _unconfirmed(liked)
        self.unlike_rejected = False
        restored_before = self.restored
        outcome = self._unlike_and_check(
            item_id,
            a,
            unlike_kind="repoint_unlike",
            verify_kind="repoint_unlike_verify",
            before=before,
            expected=before,
            # B's own like adds an entry while A still renders as B.
            still_liked=before + Counter([b]),
        )
        if self.restored > restored_before or self.unlike_rejected:
            self._undo_b_like(item_id, b)
        return outcome

    def unlike_shadow(self, action: PlannedAction, before: _LM) -> _DispatchOutcome:
        item_id, a, b = action.item_id, action.a_video_id, action.b_video_id
        if before[b] < 2:
            # The check expects B to survive the unlike; with LM showing B once,
            # unliking A could remove the song's only entry.
            self._record(
                item_id,
                "unlike_shadow_precheck",
                "skipped",
                f"YT Music shows {b} {before[b]} time(s), not twice — no shadow to remove",
            )
            return "skipped"
        liked = self._has_rating(
            item_id, "unlike_shadow_precheck", b, "like", miss="skipped", wait=False
        )
        if not liked:
            return _unconfirmed(liked)
        a_liked = self._has_rating(
            item_id, "unlike_shadow_precheck_a", a, "like", miss="skipped", wait=False
        )
        if not a_liked:
            return _unconfirmed(a_liked)
        expected = before.copy()
        expected[b] -= 1
        return self._unlike_and_check(
            item_id,
            a,
            unlike_kind="unlike_shadow_unlike",
            verify_kind="unlike_shadow_verify",
            before=before,
            expected=expected,
            still_liked=before,
        )

    def _unlike_and_check(
        self,
        item_id: int,
        a: str,
        *,
        unlike_kind: str,
        verify_kind: str,
        before: _LM,
        expected: _LM,
        still_liked: _LM,
    ) -> _DispatchOutcome:
        """Unlike A, then ``_settle`` it. If anything — Ctrl-C included — stops
        this once the unlike call has started, an ``interrupted`` row is
        committed before it propagates, marking A as possibly stranded."""
        try:
            rated = self._rate(item_id, unlike_kind, a, "none")
            if rated == "quota":
                self.unlike_rejected = True
                return "failed"  # rejected outright: nothing was sent
            return self._settle(
                item_id,
                a,
                sent=rated == "ok",
                verify_kind=verify_kind,
                before=before,
                expected=expected,
                still_liked=still_liked,
            )
        except BaseException as exc:
            try:
                self._record(
                    item_id,
                    "interrupted",
                    "failed",
                    f"{type(exc).__name__} after A's unlike call started — A may be unliked",
                )
                self.session.commit()
            except BaseException:
                # The row is lost (e.g. the DB itself failed): name A somewhere.
                print(
                    f"likesurgeon sync: stopped after unliking YouTube video {a}, unrecorded — "
                    "if the song left your YT Music Liked songs, re-like it by hand",
                    file=sys.stderr,
                )
            raise

    def _settle(
        self,
        item_id: int,
        a: str,
        *,
        sent: bool,
        verify_kind: str,
        before: _LM,
        expected: _LM,
        still_liked: _LM,
    ) -> _DispatchOutcome:
        """After an unlike that may have landed: confirm it, check LM, restore
        if needed.

        The LM check always runs. When ``getRating`` still shows A liked, the
        unlike may yet land late, so A is re-liked first — a later like wins
        over a pending unlike — and LM is checked against ``still_liked`` (A
        never unliked) instead. Anything but a matching check with A where it
        should be ends in a restore. The outcome is ``applied`` (A unliked, LM
        as expected), ``skipped`` (A confirmed liked again; counted in
        ``restored``), or ``failed`` — with A in ``left_unliked`` when its
        re-like can't be confirmed, otherwise with A unliked and LM as
        expected but some call unconfirmed.
        """
        unliked = self._has_rating(item_id, verify_kind, a, "none", miss="failed")
        if unliked is False:
            relike_ok = self._relike(item_id, a)
            lm_ok = self._check_lm(item_id, before, still_liked)  # always: baseline + audit
            if relike_ok and lm_ok:
                self.restored += 1
                return "skipped"
            # Re-like again: the unlike may have landed after the first re-like (or that failed).
        else:
            lm_ok = self._check_lm(item_id, before, expected)  # always: baseline + audit
            if lm_ok:
                return "applied" if sent and unliked else "failed"
        return self._restore(item_id, a)

    def _check_lm(self, item_id: int, before: _LM, expected: _LM) -> bool:
        """Re-read LM and compare it with ``expected``, once more after a wait
        on a mismatch; records one ``lm_check`` row.

        A match becomes the next action's baseline (``self.lm``). A failed
        read counts as a mismatch and sets ``aborted``, which stops the run
        after this action.
        """
        for attempt in range(2):
            if attempt:
                time.sleep(_LM_RECHECK_WAIT_SECONDS)
            try:
                after = _read_lm(self.ytm)
            except (UnexpectedResponseError, AuthFileMissingError) as exc:
                self.aborted = f"could not read YT Music liked songs to check an action: {exc}"
                self._record(item_id, "lm_check", "failed", f"read failed: {exc}")
                return False
            if after == expected:
                self.lm = after
                self._record(
                    item_id, "lm_check", "applied", f"as expected: {_lm_diff(before, after)}"
                )
                return True
        self._record(
            item_id,
            "lm_check",
            "failed",
            f"expected {_lm_diff(before, expected)}, got {_lm_diff(before, after)} "
            f"(also after a {_LM_RECHECK_WAIT_SECONDS}s re-read)",
        )
        return False

    def _relike(self, item_id: int, a: str) -> bool:
        """``rate(A, like)`` confirmed by ``getRating`` (``restore_like`` /
        ``restore_verify``).

        Runs even after the quota ran out: the rejected call's failed row is
        what records A as stranded for later runs (``stranded_unliked_video_ids``).
        """
        return self._rate(item_id, "restore_like", a, "like") == "ok" and (
            self._has_rating(item_id, "restore_verify", a, "like", miss="failed") is True
        )

    def _restore(self, item_id: int, a: str) -> _DispatchOutcome:
        """Re-like A after LM didn't look as expected: ``skipped`` once A is
        confirmed liked again, else ``failed`` with A left unliked."""
        if self._relike(item_id, a):
            self.restored += 1
            return "skipped"
        self.left_unliked.append(a)
        return "failed"

    def _undo_b_like(self, item_id: int, b: str) -> None:
        """Unlike the B this run liked, after A's restore (``repoint_rollback`` /
        ``repoint_rollback_verify``). A failure doesn't change the action's
        outcome — A is already back — but B is reported in ``left_liked``."""
        if (
            self._rate(item_id, "repoint_rollback", b, "none") != "ok"
            or self._has_rating(item_id, "repoint_rollback_verify", b, "none", miss="failed")
            is not True
        ):
            self.left_liked.append(b)
            if self.aborted is None:
                self.aborted = ROLLBACK_STOP

    def _rate(
        self, item_id: int, kind: str, video_id: str, rating: Literal["like", "none"]
    ) -> _RateResult:
        """``videos.rate``: ``ok`` on a 2xx; ``quota`` when the daily quota
        rejected this call — nothing was sent, and the run stops after this
        action; ``error`` otherwise, and such a call may still have landed.
        Commits its row (and the action's rows so far) right away."""
        result: _RateResult
        try:
            self.yt.rate_video(video_id, rating)
        except YouTubeQuotaExceededError as exc:
            result, reason = "quota", f"{QUOTA_REJECTED}: {exc}"
            self.quota_exhausted = True
        except YouTubeWriteError as exc:
            result, reason = "error", str(exc)
        else:
            result, reason = "ok", f"rate({rating}) ok"
        self._record(item_id, kind, "applied" if result == "ok" else "failed", reason)
        # On disk before any wait: even a hard kill from here on leaves a row
        # that makes the next sync refuse until a re-scan — and, after A's
        # unlike, marks A possibly stranded.
        self.session.commit()
        return result

    def _has_rating(
        self,
        item_id: int,
        kind: str,
        video_id: str,
        rating: Literal["like", "none"],
        *,
        miss: Literal["skipped", "failed"],
        wait: bool = True,
        miss_note: str | None = None,
    ) -> bool | None:
        """Whether ``videos.getRating`` shows ``rating`` on ``video_id`` (None if
        the read failed). A 2xx from ``videos.rate`` doesn't guarantee the
        rating changed, so each write is confirmed this way after a wait."""
        if wait:
            time.sleep(_VERIFY_WAIT_SECONDS)
        try:
            liked = self.yt.is_in_liked_videos(video_id)
        except YouTubeWriteError as exc:
            self._record(item_id, kind, "failed", str(exc))
            self.quota_exhausted |= isinstance(exc, YouTubeQuotaExceededError)
            return None
        if liked == (rating == "like"):
            self._record(item_id, kind, "applied", f"rating is {rating}")
            return True
        after = f" after +{_VERIFY_WAIT_SECONDS}s" if wait else ""
        self._record(item_id, kind, miss, miss_note or f"rating is not {rating}{after}")
        return False

    def _record(self, item_id: int, kind: str, status: str, reason: str) -> None:
        self.session.add(
            SyncAttempt(diagnosis_item_id=item_id, kind=kind, status=status, reason=reason)
        )
