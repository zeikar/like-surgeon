"""Dispatcher for playlist repoints: runs the actions ``playlist_repoint.plan`` made.

``execute`` takes mock-friendly client objects and is the only thing that
mutates a playlist finding's ``DiagnosisItem.status`` and emits its
``SyncAttempt`` rows (``playlist_*`` kinds only, so they never count toward
``sync``'s refusal).

A playlist's YT Music side renders its YouTube side in order, showing a
playable B in place of a region-blocked A. A repoint inserts B at A's YouTube
index, then deletes A's item, so YT Music shows the same list throughout. Each
write is followed by a read of both sides, compared with the baseline the
action started from: YouTube by ``(item id, video id)``, YT Music by video id
against its own baseline (it shows B where YouTube lists A). A repoint keeps
the list's length and every other slot, so a later action's indices in the
same playlist still hold, and the matching post-check reads become its
baseline.

Without such reads (a playlist's first action) both sides are read and must
equal the planning scan plus this run's applied repoints, and A and B must sit
where the action says. A playlist that fails this gets no writes this run (its
items stay ``open``) and the run moves on to other playlists. A side that
can't be read stops the run, as does YouTube's daily quota.

Invariants:
  * ``DiagnosisItem.reason`` is never modified; sync-side detail lives on
    ``SyncAttempt.reason``.
  * One ``SyncAttempt`` per write call and per check, plus a
    ``playlist_interrupted`` row when anything stops an action once A's delete
    call has started.
  * Commit cadence: per action, plus right after every write call, so a crash
    or kill at any later point still leaves that row on disk.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, NamedTuple, TypeVar

from sqlalchemy.orm import Session

from .models import DiagnosisItem, SyncAttempt
from .playlist_repoint import PlaylistAction, PlaylistState
from .sync_dispatch import QUOTA_REJECTED
from .youtube_client import YouTubeClient, YouTubeQuotaExceededError, YouTubeWriteError
from .ytmusic_client import AuthFileMissingError, UnexpectedResponseError, YTMusicClient

_Outcome = Literal["applied", "skipped", "failed"]
_WriteResult = Literal["ok", "error", "quota"]
_YouTube = tuple[tuple[str, str | None], ...]
_YTMusic = tuple[str | None, ...]
_T = TypeVar("_T")

_VERIFY_WAIT_SECONDS = 5
_RECHECK_WAIT_SECONDS = 15
_RECHECKED = f"also after a {_RECHECK_WAIT_SECONDS}s re-read"

# ``PlaylistExecResult.aborted`` after a repoint didn't go as planned.
PLAYLIST_STOP = (
    "a playlist repoint didn't go as planned, so the playlists may differ from the "
    "report — re-scan playlists before applying more"
)


@dataclass(frozen=True)
class PlaylistExecResult:
    applied: int
    failed: int
    skipped: int
    # Of ``skipped``: actions undone by putting A back.
    restored: int = 0
    # Actions left 'open' without any call: the run stopped early, or their
    # playlist is in ``stale_playlists``.
    unattempted: int = 0
    # YouTube's daily quota ran out mid-run.
    quota_exhausted: bool = False
    # Why the run stopped early (a side couldn't be read, or ``PLAYLIST_STOP``).
    aborted: str | None = None
    # Playlists whose live sides differed from the scan: no writes this run.
    stale_playlists: tuple[str, ...] = ()
    # B inserted and possibly still there although A is too.
    left_inserted: tuple[PlaylistAction, ...] = ()
    # A deleted and not put back.
    left_removed: tuple[PlaylistAction, ...] = ()
    # A's delete was sent but its item still listed or unreadable: it may land later.
    delete_unconfirmed: tuple[PlaylistAction, ...] = ()


class _Sides(NamedTuple):
    youtube: _YouTube
    ytmusic: _YTMusic


def execute(
    session: Session,
    actions: list[PlaylistAction],
    states: dict[str, PlaylistState],
    *,
    ytm: YTMusicClient,
    yt: YouTubeClient,
) -> PlaylistExecResult:
    """Apply ``actions`` in order; each write's row is committed as it happens.

    ``states`` is each planned playlist as the scan saw it. Counts are per
    finding, not per call.
    """
    run = _PlaylistRun(session, yt=yt, ytm=ytm)
    outcomes: Counter[_Outcome] = Counter()
    unattempted = 0
    for action in actions:
        if run.stopped or action.playlist_id in run.stale:
            unattempted += 1
            continue
        item = session.get_one(DiagnosisItem, action.item_id)
        base = run.take_baseline(action, states[action.playlist_id])
        if base is None:
            if run.stopped:  # unreadable: nothing was recorded for this action
                unattempted += 1
            else:
                outcomes["failed"] += 1
            session.commit()
            continue
        outcome = run.repoint(action, base)
        outcomes[outcome] += 1
        if outcome in ("applied", "skipped"):
            item.status = outcome
        session.commit()

    return PlaylistExecResult(
        applied=outcomes["applied"],
        failed=outcomes["failed"],
        skipped=outcomes["skipped"],
        unattempted=unattempted,
        quota_exhausted=run.quota_exhausted,
        aborted=run.aborted,
        stale_playlists=tuple(run.stale),
    )


def _difference(expected: Sequence[object], got: Sequence[object], label: str) -> str:
    """Where ``got`` first departs from ``expected``, e.g. ``4 items (scan: 3),
    position 2 holds 'X' (scan: 'B')``."""
    k = next(
        (i for i, (e, g) in enumerate(zip(expected, got, strict=False)) if e != g),
        min(len(expected), len(got)),
    )

    def at(seq: Sequence[object]) -> str:
        return repr(seq[k]) if k < len(seq) else "nothing"

    return (
        f"{len(got)} items ({label}: {len(expected)}), "
        f"position {k + 1} holds {at(got)} ({label}: {at(expected)})"
    )


def _misplaced(action: PlaylistAction, base: _Sides) -> list[str]:
    """Why ``base`` doesn't hold A at ``a_index`` and B at ``b_index``, if it doesn't."""
    problems = []
    p, j = action.a_index, action.b_index
    a = (action.a_playlist_item_id, action.a_video_id)
    if p >= len(base.youtube) or base.youtube[p] != a:
        held = repr(base.youtube[p]) if p < len(base.youtube) else "nothing"
        problems.append(f"YouTube side: position {p + 1} holds {held}, not A {a!r}")
    if j >= len(base.ytmusic) or base.ytmusic[j] != action.b_video_id:
        shown = repr(base.ytmusic[j]) if j < len(base.ytmusic) else "nothing"
        problems.append(
            f"YT Music side: position {j + 1} holds {shown}, not B {action.b_video_id!r}"
        )
    return problems


class _ReadFailed(Exception):
    """A side couldn't be read; ``quota`` when YouTube's daily quota refused it."""

    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        self.quota = quota


class _PlaylistRun:
    """State one ``execute`` shares across its actions."""

    def __init__(self, session: Session, *, yt: YouTubeClient, ytm: YTMusicClient) -> None:
        self.session = session
        self.yt = yt
        self.ytm = ytm
        # Per playlist: both sides as expected now — the scan plus this run's
        # applied repoints.
        self.expected: dict[str, _Sides] = {}
        # Playlists whose last action's checks just read them as expected.
        self.confirmed: set[str] = set()
        self.stale: list[str] = []
        self.quota_exhausted = False
        self.aborted: str | None = None

    @property
    def stopped(self) -> bool:
        return self.quota_exhausted or self.aborted is not None

    def take_baseline(self, action: PlaylistAction, scanned: PlaylistState) -> _Sides | None:
        """The sides ``action`` is checked against, or None: the playlist
        differs from the scan plus this run's repoints, or A or B isn't where
        the action says (either way added to ``stale``), or a side couldn't be
        read (the run stops).

        Both sides are read unless the playlist's previous action just
        confirmed them; taking the baseline marks them unconfirmed again.
        """
        pid = action.playlist_id
        base = self.expected.setdefault(pid, _Sides(scanned.youtube, scanned.ytmusic))
        problems = []
        read = pid not in self.confirmed
        self.confirmed.discard(pid)
        if read:
            try:
                youtube = self._read_youtube(pid)
                ytmusic = self._read_ytmusic(pid)
            except _ReadFailed as exc:
                self._read_failed(action, exc, "before its next repoint")
                return None
            if youtube != base.youtube:
                problems.append(f"YouTube side: {_difference(base.youtube, youtube, 'scan')}")
            if ytmusic != base.ytmusic:
                problems.append(f"YT Music side: {_difference(base.ytmusic, ytmusic, 'scan')}")
        if not problems:
            problems = _misplaced(action, base)
        if problems:
            self.stale.append(pid)
            self._record(action.item_id, "playlist_precheck", "failed", "; ".join(problems))
            return None
        if read:
            self._record(
                action.item_id, "playlist_precheck", "applied", "both sides match the scan"
            )
        return base

    def repoint(self, action: PlaylistAction, base: _Sides) -> _Outcome:
        """Insert B at A's index, check both sides, delete A's item, check again."""
        p, b = action.a_index, action.b_video_id
        result, new_id = self._write(
            action.item_id,
            "playlist_insert",
            lambda: self.yt.insert_playlist_item(action.playlist_id, b, p),
            lambda item_id: f"inserted {b} at position {p + 1} as item {item_id}",
        )
        if result == "quota":
            return "failed"  # nothing was sent
        if new_id is None:
            return self._halt()
        b_item = (new_id, b)
        time.sleep(_VERIFY_WAIT_SECONDS)
        inserted = base.youtube[:p] + (b_item,) + base.youtube[p:]
        if self._youtube_after(action, "playlist_insert_verify", inserted) != inserted:
            return self._halt()
        j = action.b_index
        # YT Music shows B at j already; one more B there reads the same on either side of it.
        shown = base.ytmusic[:j] + (b,) + base.ytmusic[j:]
        if self._ytmusic_after(action, "playlist_ytm_verify", shown) != shown:
            return self._halt()

        with self._interrupt_marks_delete(action):
            result, _ = self._write(
                action.item_id,
                "playlist_delete",
                lambda: self.yt.delete_playlist_item(action.a_playlist_item_id),
                lambda _: f"deleted item {action.a_playlist_item_id} ({action.a_video_id})",
            )
            if result != "ok":
                return self._halt()
            time.sleep(_VERIFY_WAIT_SECONDS)
            replaced = base.youtube[:p] + (b_item,) + base.youtube[p + 1 :]
            if self._youtube_after(action, "playlist_delete_verify", replaced) != replaced:
                return self._halt()
            # Known limit: a YT Music read served from before the insert would
            # pass too — as with the likes repoint's "LM unchanged" check.
            if self._ytmusic_after(action, "playlist_check", base.ytmusic) != base.ytmusic:
                return self._halt()
            self.expected[action.playlist_id] = _Sides(replaced, base.ytmusic)
            self.confirmed.add(action.playlist_id)
            return "applied"

    def _halt(self) -> _Outcome:
        """Stop the run after a step that didn't go as planned, unless a read
        failure or the quota already stopped it (that cause stays visible)."""
        if not self.stopped:
            self.aborted = PLAYLIST_STOP
        return "failed"

    @contextmanager
    def _interrupt_marks_delete(self, action: PlaylistAction) -> Iterator[None]:
        """Whatever stops the body — Ctrl-C included — commits a
        ``playlist_interrupted`` row before propagating, marking A as possibly
        removed."""
        try:
            yield
        except BaseException as exc:
            try:
                self._record(
                    action.item_id,
                    "playlist_interrupted",
                    "failed",
                    f"{type(exc).__name__} after A's delete call started — A may be removed",
                )
                self.session.commit()
            except BaseException:
                # The row is lost (e.g. the DB itself failed): name A somewhere.
                print(
                    f"likesurgeon: stopped after deleting YouTube video {action.a_video_id} "
                    f"from playlist {action.playlist_title!r}, unrecorded — if it is missing, "
                    "add it back by hand",
                    file=sys.stderr,
                )
            raise

    def _write(
        self,
        item_id: int,
        kind: str,
        call: Callable[[], _T],
        done: Callable[[_T], str],
    ) -> tuple[_WriteResult, _T | None]:
        """A playlist write: ``ok`` with its return value; ``quota`` when the
        daily quota rejected it (nothing was sent); ``error`` otherwise, and
        such a call may still have landed. Commits its row right away."""
        try:
            value = call()
        except YouTubeQuotaExceededError as exc:
            self.quota_exhausted = True
            self._record(item_id, kind, "failed", f"{QUOTA_REJECTED}: {exc}")
            result: tuple[_WriteResult, _T | None] = ("quota", None)
        except YouTubeWriteError as exc:
            self._record(item_id, kind, "failed", str(exc))
            result = ("error", None)
        else:
            self._record(item_id, kind, "applied", done(value))
            result = ("ok", value)
        # On disk before any wait: even a hard kill from here on leaves the row.
        self.session.commit()
        return result

    def _youtube_after(
        self, action: PlaylistAction, kind: str, expected: _YouTube
    ) -> _YouTube | None:
        return self._side_after(
            action,
            kind,
            expected,
            lambda: self._read_youtube(action.playlist_id),
            "YouTube side",
        )

    def _ytmusic_after(
        self, action: PlaylistAction, kind: str, expected: _YTMusic
    ) -> _YTMusic | None:
        return self._side_after(
            action,
            kind,
            expected,
            lambda: self._read_ytmusic(action.playlist_id),
            "YT Music side",
        )

    def _side_after(
        self,
        action: PlaylistAction,
        kind: str,
        expected: Sequence[object],
        read: Callable[[], _T],
        side: str,
    ) -> _T | None:
        """Read a side, once more after a wait on a mismatch, and record one
        ``kind`` row. Returns the last read, or None if it failed (which
        stops the run)."""
        for attempt in range(2):
            if attempt:
                time.sleep(_RECHECK_WAIT_SECONDS)
            try:
                got = read()
            except _ReadFailed as exc:
                self._record(action.item_id, kind, "failed", f"read failed: {exc}")
                self._read_failed(action, exc, "to check a repoint")
                return None
            if got == expected:
                self._record(action.item_id, kind, "applied", f"{side} as expected")
                return got
        self._record(
            action.item_id,
            kind,
            "failed",
            f"{side}: {_difference(expected, got, 'expected')} ({_RECHECKED})",
        )
        return got

    def _read_youtube(self, playlist_id: str) -> _YouTube:
        try:
            return tuple(self.yt.read_playlist_items(playlist_id))
        except YouTubeQuotaExceededError as exc:
            raise _ReadFailed(f"YouTube: {exc}", quota=True) from exc
        except YouTubeWriteError as exc:
            raise _ReadFailed(f"YouTube: {exc}") from exc

    def _read_ytmusic(self, playlist_id: str) -> _YTMusic:
        """The YT Music side as video ids."""
        try:
            tracks = self.ytm.fetch_playlist(playlist_id)["tracks"]
            # Skipping a track would shift every later position.
            if not all(isinstance(t, dict) for t in tracks):
                raise UnexpectedResponseError(
                    f"ytmusicapi.get_playlist({playlist_id}) returned a track that is not a dict"
                )
        except (UnexpectedResponseError, AuthFileMissingError) as exc:
            raise _ReadFailed(f"YT Music: {exc}") from exc
        return tuple(t.get("videoId") for t in tracks)

    def _read_failed(self, action: PlaylistAction, exc: _ReadFailed, when: str) -> None:
        """Stop the run: through ``quota_exhausted`` on the quota, else ``aborted``."""
        if exc.quota:
            self.quota_exhausted = True
        elif self.aborted is None:
            self.aborted = f"could not read playlist {action.playlist_title!r} {when}: {exc}"

    def _record(self, item_id: int, kind: str, status: str, reason: str) -> None:
        self.session.add(
            SyncAttempt(diagnosis_item_id=item_id, kind=kind, status=status, reason=reason)
        )
