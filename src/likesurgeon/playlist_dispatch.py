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
against its own baseline (it shows B where YouTube lists A).

Every check but the one after removing B (the baseline again, exactly) has
one form, ``_only_change``: the read holds one moving element — B's item on
YouTube, one more or one moved B on YT Music, A's new item after putting A
back — and with that occurrence removed it is exactly the expected list,
order included, so nothing else may move, appear or go. A playlist is
``positional`` (the default), where that element must also sit at the index
the step puts it (B at A's index; on YT Music next to or at B's index), or
``sorted``, where it may sit anywhere: YouTube puts it where the playlist's
sort order says.

An automatically sorted playlist refuses an insert with a position (HTTP 400
``manualSortRequired``, seen live). That insert is recorded as not sent
(``MANUAL_SORT_REJECTED``, like a quota rejection), the playlist is
``sorted`` for the rest of the run, and the insert is sent again without a
position; later inserts there, B's or A's when putting it back, go without
one at once. Each confirmed repoint's post-check reads become its playlist's
next baseline, and its removed and landed indices on both sides (equal in a
positional playlist, so nothing shifts) move a later action's A and B
indices before that action's precheck.

Until A's delete is sent, a check that fails removes B again — only the item
this action added, by its item id (found by reading the YouTube side when the
insert call errored) — and confirms the YouTube side is the baseline again. A
B that may be in the playlist but can't be identified, removed or confirmed
gone is reported in ``left_inserted``; so is an accepted insert that still
isn't listed, as it may land later. Only an errored insert that landed where
it was asked to (A's index; anywhere in a sorted playlist) and was cleanly
removed lets the run go on; everything else stops it (an errored insert not
seen after the re-read may still land, so it is reported too). A
``MANUAL_SORT_REJECTED`` insert is not such a case: it was refused outright.

After A's delete is sent, B is never removed while A's item may still be
listed: a delete the YouTube side can't confirm (A's item still listed after
the re-read, or the side unreadable) may yet land, so nothing is undone, the
pair is reported in ``delete_unconfirmed`` and the run stops. With A's item
gone but either side off, or YT Music unreadable, A is inserted again (at its
index, or where a sorted playlist puts it; new item id either way) and B is
removed only once A is confirmed back; an A that isn't is reported in
``left_removed`` with B left in place. A delete call that errored but landed,
with both checks passing, is not ``applied`` (a call failed) but lets the
run go on.

Without such reads (a playlist's first action) both sides are read and must
equal the planning scan plus this run's applied repoints, and A and B must sit
where the action says. A playlist that fails this gets no writes this run (its
items stay ``open``) and the run moves on to other playlists. A side that
can't be read stops the run, as does YouTube's daily quota.

Row kinds: ``playlist_precheck``; ``playlist_insert`` (two rows when a sorted
playlist refused the position), ``playlist_insert_verify``,
``playlist_ytm_verify``; ``playlist_delete``, ``playlist_delete_verify``,
``playlist_check``; ``playlist_rollback``, ``playlist_rollback_verify``
(removing B); ``playlist_restore``, ``playlist_restore_verify`` (putting A
back); ``playlist_interrupted``.

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
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple, TypeVar

from sqlalchemy.orm import Session

from .models import DiagnosisItem, SyncAttempt
from .playlist_repoint import PlaylistAction, PlaylistState
from .sync_dispatch import QUOTA_REJECTED
from .youtube_client import (
    PlaylistManualSortRequired,
    YouTubeClient,
    YouTubeQuotaExceededError,
    YouTubeWriteError,
)
from .ytmusic_client import AuthFileMissingError, UnexpectedResponseError, YTMusicClient

_Outcome = Literal["applied", "skipped", "failed"]
_WriteResult = Literal["ok", "error", "quota", "manual_sort"]
_YouTube = tuple[tuple[str, str | None], ...]
_YTMusic = tuple[str | None, ...]
_T = TypeVar("_T")

_VERIFY_WAIT_SECONDS = 5
_RECHECK_WAIT_SECONDS = 15
_RECHECKED = f"also after a {_RECHECK_WAIT_SECONDS}s re-read"

# A write row's reason prefix when an automatically sorted playlist refused an
# insert with a position: definitively not sent, like ``QUOTA_REJECTED``.
MANUAL_SORT_REJECTED = "not sent, the playlist is sorted automatically"

# ``PlaylistExecResult.aborted`` after a repoint didn't go as planned.
PLAYLIST_STOP = (
    "a playlist repoint didn't go as planned, so the playlists may differ from the "
    "report — re-scan playlists before applying more"
)


@dataclass(frozen=True)
class PlaylistExecResult:
    applied: int
    failed: int
    skipped: int  # YT Music didn't show the inserted B (B removed again), or A was put back
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
    # Repoints whose checks passed in a sorted playlist (``applied``, or
    # ``failed`` when the delete call errored but landed): B sits where the
    # playlist's sort order puts it, not at A's old position.
    reordered: int = 0
    # Titles of the playlists found sorted automatically this run.
    sorted_playlists: tuple[str, ...] = ()
    # (playlist title, A's 1-based position, B): B inserted for A and possibly
    # still in the playlist (next to A, or where a sorted playlist puts it);
    # the user removes it by hand.
    left_inserted: tuple[tuple[str, int, str], ...] = ()
    # (playlist title, A's 1-based position, A): A deleted and possibly not
    # back; B stays in. The user adds A back by hand.
    left_removed: tuple[tuple[str, int, str], ...] = ()
    # (playlist title, A's 1-based position, A, B): A's delete was sent but the
    # YouTube side still lists A's item or couldn't be read. The delete may
    # still land, so nothing was undone: B stays in.
    delete_unconfirmed: tuple[tuple[str, int, str, str], ...] = ()


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
        taken = run.take_baseline(action, states[action.playlist_id])
        if taken is None:
            if run.stopped:  # unreadable: nothing was recorded for this action
                unattempted += 1
            else:
                outcomes["failed"] += 1
            session.commit()
            continue
        outcome = run.repoint(*taken)
        outcomes[outcome] += 1
        if outcome in ("applied", "skipped"):
            item.status = outcome
        session.commit()

    return PlaylistExecResult(
        applied=outcomes["applied"],
        failed=outcomes["failed"],
        skipped=outcomes["skipped"],
        restored=run.restored,
        reordered=run.reordered,
        sorted_playlists=tuple(run.sorted.values()),
        unattempted=unattempted,
        quota_exhausted=run.quota_exhausted,
        aborted=run.aborted,
        stale_playlists=tuple(run.stale),
        left_inserted=tuple(run.left_inserted),
        left_removed=tuple(run.left_removed),
        delete_unconfirmed=tuple(run.delete_unconfirmed),
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


def _only_change(
    got: tuple[object, ...], rest: tuple[object, ...], x: object, at: int | None = None
) -> int | None:
    """The index of the one occurrence of ``x`` in ``got`` that, removed,
    leaves exactly ``rest`` (order included); None if there is none. With
    ``at`` that occurrence must sit at index ``at``; without it (a sorted
    playlist) anywhere. Any other item moved, added or missing fails both."""
    if len(got) != len(rest) + 1:
        return None
    candidates = range(len(got)) if at is None else [at] if at < len(got) else []
    for i in candidates:
        if got[i] == x and got[:i] + got[i + 1 :] == rest:
            return i
    return None


class _Change(NamedTuple):
    """A side that must read as ``rest`` plus one ``x`` (``_only_change``)."""

    rest: tuple[object, ...]
    x: object
    at: int | None  # x's index; None in a sorted playlist: anywhere

    def index(self, got: tuple[object, ...]) -> int | None:
        return _only_change(got, self.rest, self.x, self.at)

    def mismatch(self, got: tuple[object, ...]) -> str | None:
        """Why ``got`` isn't this change, or None if it is."""
        if self.index(got) is not None:
            return None
        if self.at is not None:
            expected = self.rest[: self.at] + (self.x,) + self.rest[self.at :]
            return _difference(expected, got, "expected")
        if self.x not in got:
            return f"{self.x!r} isn't listed; {_difference(self.rest, got, 'expected without it')}"
        i = got.index(self.x)
        without = got[:i] + got[i + 1 :]
        return (
            f"without {self.x!r} at position {i + 1}: {_difference(self.rest, without, 'expected')}"
        )


def _unchanged(expected: tuple[object, ...]) -> Callable[[tuple[object, ...]], str | None]:
    """The ``mismatch`` of a side that must read exactly ``expected``."""
    return lambda got: None if got == expected else _difference(expected, got, "expected")


def _added_item(base: _YouTube, got: _YouTube, video_id: str) -> tuple[str, str | None] | None:
    """The one item ``got`` adds to ``base`` anywhere, if that is all that
    changed and it is ``video_id``."""
    new = [it for it in got if it not in base]
    if len(new) == 1 and new[0][1] == video_id and _only_change(got, base, new[0]) is not None:
        return new[0]
    return None


def _shifted(index: int, removed: int, landed: int) -> int:
    """Where the entry at ``index`` sits once the one at ``removed`` left and
    one landed at ``landed`` (an index in the new list). The identity when
    ``removed == landed``, as in a positional repoint."""
    kept = index - (index > removed)
    return kept + (kept >= landed)


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
        # applied repoints (the reads that confirmed the last one).
        self.expected: dict[str, _Sides] = {}
        # Per playlist, each applied repoint's indices: (YouTube removed,
        # landed, YT Music removed, landed).
        self.moves: dict[str, list[tuple[int, int, int, int]]] = {}
        # Playlists found sorted automatically: id -> title, in the order found.
        self.sorted: dict[str, str] = {}
        self.reordered = 0
        # Playlists whose last action's checks just read them as expected.
        self.confirmed: set[str] = set()
        self.stale: list[str] = []
        self.left_inserted: list[tuple[str, int, str]] = []
        self.left_removed: list[tuple[str, int, str]] = []
        self.delete_unconfirmed: list[tuple[str, int, str, str]] = []
        self.restored = 0
        self.quota_exhausted = False
        self.aborted: str | None = None

    @property
    def stopped(self) -> bool:
        return self.quota_exhausted or self.aborted is not None

    def take_baseline(
        self, action: PlaylistAction, scanned: PlaylistState
    ) -> tuple[PlaylistAction, _Sides] | None:
        """``action`` with its indices moved by this run's repoints in its
        playlist, and the sides it is checked against; or None: the playlist
        differs from the scan plus those repoints, or A or B isn't where the
        moved action says (either way added to ``stale``), or a side couldn't
        be read (the run stops).

        Both sides are read unless the playlist's previous action just
        confirmed them; taking the baseline marks them unconfirmed again.
        """
        pid = action.playlist_id
        p, j = action.a_index, action.b_index
        for removed, landed, removed_j, landed_j in self.moves.get(pid, ()):
            p, j = _shifted(p, removed, landed), _shifted(j, removed_j, landed_j)
        action = replace(action, a_index=p, b_index=j)
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
        return action, base

    def _at(self, action: PlaylistAction, index: int) -> int | None:
        """Where a check wants the moving element: at ``index`` in a positional
        playlist, anywhere (None) in a sorted one."""
        return None if action.playlist_id in self.sorted else index

    def repoint(self, action: PlaylistAction, base: _Sides) -> _Outcome:
        """Insert B at A's index (or where a sorted playlist puts it), check
        both sides, delete A's item, check again.

        Until A's delete is sent, anything off removes B again (``_remove_b``).
        """
        p, j, b = action.a_index, action.b_index, action.b_video_id
        result, new_id = self._insert(action, "playlist_insert", b, p)
        if result == "quota":
            return "failed"  # nothing was sent
        time.sleep(_VERIFY_WAIT_SECONDS)
        if new_id is None:
            return self._settle_errored_insert(action, base)
        b_item = (new_id, b)
        inserted = _Change(base.youtube, b_item, self._at(action, p))
        youtube = self._read_settled(
            action,
            "playlist_insert_verify",
            lambda: self._read_youtube(action.playlist_id),
            lambda got: inserted.index(got) is not None,
        )
        if youtube is None:
            self._remove_b(action, base, new_id)
            return "failed"
        if b_item not in youtube:
            # Accepted, so it may still land: B is reported, not counted as absent.
            self._record(
                action.item_id,
                "playlist_insert_verify",
                "failed",
                f"B's item {new_id} isn't listed ({_RECHECKED}) — the insert may still land",
            )
            self._report_inserted(action)
            return "failed"
        self._record_check(
            action, "playlist_insert_verify", "YouTube side", inserted.mismatch(youtube)
        )
        if inserted.index(youtube) is None:
            self._remove_b(action, base, new_id)
            return self._halt()
        # YT Music shows B at j already; in a positional playlist one more B
        # there reads the same on either side of it, so ``at`` j covers both.
        shown = _Change(base.ytmusic, b, self._at(action, j))
        ytmusic = self._check_ytmusic(action, "playlist_ytm_verify", shown.mismatch)
        if ytmusic is None or shown.index(ytmusic) is None:
            removed = self._remove_b(action, base, new_id)
            self._halt()
            # Terminal only when YT Music was read: the pair shows wrong, retrying repeats it.
            return "skipped" if removed and ytmusic is not None else "failed"

        with self._interrupt_marks_delete(action):
            result, _ = self._write(
                action.item_id,
                "playlist_delete",
                lambda: self.yt.delete_playlist_item(action.a_playlist_item_id),
                lambda _: f"deleted item {action.a_playlist_item_id} ({action.a_video_id})",
            )
            if result != "quota":
                return self._settle_delete(action, base, b_item, sent_ok=result == "ok")
        # Rejected outright: nothing was sent, so A is in place and B can go.
        self._remove_b(action, base, new_id)
        return "failed"

    def _settle_delete(
        self, action: PlaylistAction, base: _Sides, b_item: tuple[str, str], *, sent_ok: bool
    ) -> _Outcome:
        """After A's delete was sent (``sent_ok``: with a 2xx): confirm A's item
        gone with B in (in its slot, unless the playlist is sorted), then YT
        Music showing B once instead of twice. While A's item may still be
        listed nothing is undone — the delete may yet land, and removing B
        then would leave neither. Once it is gone, a side that is off or a YT
        Music side that is unreadable puts A back (``_restore``)."""
        pid, p, j = action.playlist_id, action.a_index, action.b_index
        replaced = _Change(base.youtube[:p] + base.youtube[p + 1 :], b_item, self._at(action, p))
        time.sleep(_VERIFY_WAIT_SECONDS)
        youtube = self._check_youtube(action, "playlist_delete_verify", replaced.mismatch)
        if youtube is None or action.a_playlist_item_id in {item_id for item_id, _ in youtube}:
            self.delete_unconfirmed.append(
                (action.playlist_title, p + 1, action.a_video_id, action.b_video_id)
            )
            return self._halt()
        landed = replaced.index(youtube)
        if landed is None:
            return self._restore(action, base, b_item)
        # YT Music no longer shows B for A at j, only for B's own item: at j in
        # a positional playlist (the list reads as before), or wherever a
        # sorted one put it. Known limit: a YT Music read served from before
        # the insert would pass too — as with the likes repoint's "LM
        # unchanged" check.
        shown = _Change(
            base.ytmusic[:j] + base.ytmusic[j + 1 :], action.b_video_id, self._at(action, j)
        )
        ytmusic = self._check_ytmusic(action, "playlist_check", shown.mismatch)
        landed_j = shown.index(ytmusic) if ytmusic is not None else None
        if landed_j is None:
            return self._restore(action, base, b_item)
        # Both sides as checked, so later actions in this playlist build on them.
        self.expected[pid] = _Sides(youtube, ytmusic)
        self.moves.setdefault(pid, []).append((p, landed, j, landed_j))
        self.confirmed.add(pid)
        if pid in self.sorted:
            self.reordered += 1
        return "applied" if sent_ok else "failed"

    def _restore(self, action: PlaylistAction, base: _Sides, b_item: tuple[str, str]) -> _Outcome:
        """Insert A again and, once it is confirmed back with every other item
        unchanged, remove B. A comes back under a new item id. In a positional
        playlist A must be back at its index with B right after it; in a
        sorted one each may sit anywhere. An A not confirmed back keeps B in
        and is reported in ``left_removed``. Always stops the run."""
        p, a = action.a_index, action.a_video_id
        result, _ = self._insert(action, "playlist_restore", a, p)
        if result in ("ok", "error"):  # an errored insert may have landed too
            time.sleep(_VERIFY_WAIT_SECONDS)
            at = self._at(action, p)  # after the insert: it may have found the playlist sorted
            without_a = base.youtube[:p] + base.youtube[p + 1 :]

            def mismatch(got: _YouTube) -> str | None:
                """None when ``got`` holds one new item of A (at ``at``) and,
                that set aside, is ``without_a`` plus B's item (at ``at``,
                i.e. right after A, in a positional playlist)."""
                new = [it for it in got if it[1] == a and it not in base.youtube]
                if len(new) != 1:
                    return f"{len(new)} new items of A {a!r} listed (expected 1)"
                i = got.index(new[0])
                if at is not None and i != at:
                    return f"A's new item {new[0]!r} is at position {i + 1}, not {at + 1}"
                return _Change(without_a, b_item, at).mismatch(got[:i] + got[i + 1 :])

            youtube = self._check_youtube(action, "playlist_restore_verify", mismatch)
            if youtube is not None and mismatch(youtube) is None:
                # The baseline with A's new item id (and, if sorted, A's new place).
                restored = tuple(it for it in youtube if it != b_item)
                removed = self._remove_b(action, _Sides(restored, base.ytmusic), b_item[0])
                self._halt()
                if removed:
                    self.restored += 1
                    return "skipped"
                return "failed"
        self.left_removed.append((action.playlist_title, p + 1, a))
        return self._halt()

    def _settle_errored_insert(self, action: PlaylistAction, base: _Sides) -> _Outcome:
        """After an insert call that errored, so B's item id is unknown: B
        landed if the YouTube side is the baseline plus exactly one item of B,
        which is then removed. A timeout doesn't prove rejection, so B unseen after
        the re-read may still land: it is reported and the run stops. The run
        goes on only if B landed where it was asked to (A's index; anywhere in
        a sorted playlist) and was cleanly removed."""
        b, at = action.b_video_id, self._at(action, action.a_index)

        def as_asked(got: _YouTube) -> bool:
            added = _added_item(base.youtube, got, b)
            return added is not None and _only_change(got, base.youtube, added, at) is not None

        # "Unchanged" is only concluded after the re-read: the insert may land late.
        youtube = self._read_settled(
            action,
            "playlist_insert_verify",
            lambda: self._read_youtube(action.playlist_id),
            as_asked,
        )
        if youtube is None:
            self._report_inserted(action)
            return "failed"
        if youtube == base.youtube:
            self._record(
                action.item_id,
                "playlist_insert_verify",
                "failed",
                f"B isn't listed ({_RECHECKED}) — the insert call errored, so it may still land",
            )
            self._report_inserted(action)
            return "failed"
        added = _added_item(base.youtube, youtube, b)
        if added is None:
            self._record(
                action.item_id,
                "playlist_insert_verify",
                "failed",
                f"YouTube side: {_difference(base.youtube, youtube, 'baseline')} ({_RECHECKED}) "
                "— can't tell which item the errored insert added",
            )
            self._report_inserted(action)
            return "failed"
        self._record(
            action.item_id,
            "playlist_insert_verify",
            "failed",
            f"B listed as item {added[0]} at position {youtube.index(added) + 1} "
            "although the insert call errored",
        )
        if self._remove_b(action, base, added[0]) and not as_asked(youtube):
            self._halt()
        return "failed"

    def _remove_b(self, action: PlaylistAction, base: _Sides, b_item_id: str) -> bool:
        """Delete the B item this action added, by its item id, while A is still
        in place, and confirm the YouTube side is the baseline again. A B that
        may still be listed is reported (``_report_inserted``); a B gone with
        something else changed only stops the run."""
        result, _ = self._write(
            action.item_id,
            "playlist_rollback",
            lambda: self.yt.delete_playlist_item(b_item_id),
            lambda _: f"deleted item {b_item_id} ({action.b_video_id})",
        )
        if result == "ok":
            time.sleep(_VERIFY_WAIT_SECONDS)
            youtube = self._check_youtube(
                action, "playlist_rollback_verify", _unchanged(base.youtube)
            )
            if youtube == base.youtube:
                return True
            if youtube is not None and b_item_id not in {item_id for item_id, _ in youtube}:
                self._halt()
                return False
        self._report_inserted(action)
        return False

    def _report_inserted(self, action: PlaylistAction) -> None:
        """B may be in the playlist: report it and stop the run."""
        self.left_inserted.append((action.playlist_title, action.a_index + 1, action.b_video_id))
        self._halt()

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
                    f"{type(exc).__name__} after A's delete call started — A may be missing "
                    "or B may be extra; re-scan playlists",
                )
                self.session.commit()
            except BaseException:
                # The row is lost (e.g. the DB itself failed): name A somewhere.
                print(
                    f"likesurgeon: stopped after deleting YouTube video {action.a_video_id} "
                    f"from playlist {action.playlist_title!r}, unrecorded — if it is missing, "
                    f"add it back by hand; if it is there, remove {action.b_video_id} next to it",
                    file=sys.stderr,
                )
            raise

    def _insert(
        self, action: PlaylistAction, kind: str, video_id: str, position: int
    ) -> tuple[_WriteResult, str | None]:
        """Insert ``video_id`` at ``position``, or without a position once the
        playlist is known to be sorted. A positional insert refused because
        the playlist is sorted automatically makes it sorted for the rest of
        the run and is sent again without a position (a second ``kind`` row)."""
        pid = action.playlist_id
        if pid not in self.sorted:
            result = self._write(
                action.item_id,
                kind,
                lambda: self.yt.insert_playlist_item(pid, video_id, position),
                lambda item_id: f"inserted {video_id} at position {position + 1} as item {item_id}",
            )
            if result[0] != "manual_sort":
                return result
            self.sorted[pid] = action.playlist_title
        return self._write(
            action.item_id,
            kind,
            lambda: self.yt.insert_playlist_item(pid, video_id),
            lambda item_id: (
                f"inserted {video_id} as item {item_id}, where the playlist's sort order puts it"
            ),
        )

    def _write(
        self,
        item_id: int,
        kind: str,
        call: Callable[[], _T],
        done: Callable[[_T], str],
    ) -> tuple[_WriteResult, _T | None]:
        """A playlist write: ``ok`` with its return value; ``quota`` when the
        daily quota rejected it, ``manual_sort`` when an automatically sorted
        playlist refused a position (either way nothing was sent); ``error``
        otherwise, and such a call may still have landed. Commits its row
        right away."""
        try:
            value = call()
        except YouTubeQuotaExceededError as exc:
            self.quota_exhausted = True
            self._record(item_id, kind, "failed", f"{QUOTA_REJECTED}: {exc}")
            result: tuple[_WriteResult, _T | None] = ("quota", None)
        except PlaylistManualSortRequired as exc:
            self._record(item_id, kind, "failed", f"{MANUAL_SORT_REJECTED}: {exc}")
            result = ("manual_sort", None)
        except YouTubeWriteError as exc:
            self._record(item_id, kind, "failed", str(exc))
            result = ("error", None)
        else:
            self._record(item_id, kind, "applied", done(value))
            result = ("ok", value)
        # On disk before any wait: even a hard kill from here on leaves the row.
        self.session.commit()
        return result

    def _check_youtube(
        self, action: PlaylistAction, kind: str, mismatch: Callable[[_YouTube], str | None]
    ) -> _YouTube | None:
        return self._check(
            action, kind, "YouTube side", lambda: self._read_youtube(action.playlist_id), mismatch
        )

    def _check_ytmusic(
        self, action: PlaylistAction, kind: str, mismatch: Callable[[_YTMusic], str | None]
    ) -> _YTMusic | None:
        return self._check(
            action, kind, "YT Music side", lambda: self._read_ytmusic(action.playlist_id), mismatch
        )

    def _check(
        self,
        action: PlaylistAction,
        kind: str,
        side: str,
        read: Callable[[], _T],
        mismatch: Callable[[_T], str | None],
    ) -> _T | None:
        """Read a side until ``mismatch`` finds nothing wrong
        (``_read_settled``) and record one ``kind`` row. Returns the last
        read, or None if it failed."""
        got = self._read_settled(action, kind, read, lambda got: mismatch(got) is None)
        if got is not None:
            self._record_check(action, kind, side, mismatch(got))
        return got

    def _read_settled(
        self,
        action: PlaylistAction,
        kind: str,
        read: Callable[[], _T],
        accept: Callable[[_T], bool],
    ) -> _T | None:
        """Read a side, once more after a wait if ``accept`` rejects it; returns
        the last read. A failed read records a ``kind`` row, stops the run and
        returns None."""
        for attempt in range(2):
            if attempt:
                time.sleep(_RECHECK_WAIT_SECONDS)
            try:
                got = read()
            except _ReadFailed as exc:
                self._record(action.item_id, kind, "failed", f"read failed: {exc}")
                self._read_failed(action, exc, "to check a repoint")
                return None
            if accept(got):
                break
        return got

    def _record_check(
        self, action: PlaylistAction, kind: str, side: str, problem: str | None
    ) -> None:
        if problem is None:
            self._record(action.item_id, kind, "applied", f"{side} as expected")
        else:
            self._record(action.item_id, kind, "failed", f"{side}: {problem} ({_RECHECKED})")

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
