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

Until A's delete is sent, a check that fails removes B again — only the item
this action added, by its item id (found by reading the YouTube side when the
insert call errored) — and confirms the YouTube side is the baseline again. A
B that may be in the playlist but can't be identified, removed or confirmed
gone is reported in ``left_inserted``; so is an accepted insert that still
isn't listed, as it may land later. Only an errored insert that didn't land,
or that landed at A's index and was cleanly removed, lets the run go on;
everything else stops it.

After A's delete is sent, B is never removed while A's item may still be
listed: a delete the YouTube side can't confirm (A's item still listed after
the re-read, or the side unreadable) may yet land, so nothing is undone, the
pair is reported in ``delete_unconfirmed`` and the run stops. With A's item
gone but YT Music off or unreadable, A is inserted again at its index (new
item id, so that check compares video ids) and B is removed only once A is
confirmed back; an A that isn't is reported in ``left_removed`` with B left in
its slot. A delete call that errored but landed, with both checks passing, is
not ``applied`` (a call failed) but lets the run go on.

Without such reads (a playlist's first action) both sides are read and must
equal the planning scan plus this run's applied repoints, and A and B must sit
where the action says. A playlist that fails this gets no writes this run (its
items stay ``open``) and the run moves on to other playlists. A side that
can't be read stops the run, as does YouTube's daily quota.

Row kinds: ``playlist_precheck``; ``playlist_insert``,
``playlist_insert_verify``, ``playlist_ytm_verify``; ``playlist_delete``,
``playlist_delete_verify``, ``playlist_check``; ``playlist_rollback``,
``playlist_rollback_verify`` (removing B); ``playlist_restore``,
``playlist_restore_verify`` (putting A back); ``playlist_interrupted``.

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
    # (playlist title, A's 1-based position, B): B inserted for A and possibly
    # still in the playlist next to A; the user removes it by hand.
    left_inserted: tuple[tuple[str, int, str], ...] = ()
    # (playlist title, A's 1-based position, A): A deleted and possibly not
    # back; B stays in its slot. The user adds A back by hand.
    left_removed: tuple[tuple[str, int, str], ...] = ()
    # (playlist title, A's 1-based position, A, B): A's delete was sent but the
    # YouTube side still lists A's item or couldn't be read. The delete may
    # still land, so nothing was undone: B stays at A's position.
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
        restored=run.restored,
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


def _added_item(base: _YouTube, got: _YouTube, video_id: str) -> tuple[str, str | None] | None:
    """The one item ``got`` adds to ``base``, if that is all that changed and it
    is ``video_id``."""
    new = [it for it in got if it not in base]
    if len(new) == 1 and new[0][1] == video_id and tuple(it for it in got if it != new[0]) == base:
        return new[0]
    return None


def _video_ids(items: _YouTube) -> tuple[str | None, ...]:
    return tuple(video_id for _, video_id in items)


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
        self.left_inserted: list[tuple[str, int, str]] = []
        self.left_removed: list[tuple[str, int, str]] = []
        self.delete_unconfirmed: list[tuple[str, int, str, str]] = []
        self.restored = 0
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
        """Insert B at A's index, check both sides, delete A's item, check again.

        Until A's delete is sent, anything off removes B again (``_remove_b``).
        """
        p, b = action.a_index, action.b_video_id
        result, new_id = self._write(
            action.item_id,
            "playlist_insert",
            lambda: self.yt.insert_playlist_item(action.playlist_id, b, p),
            lambda item_id: f"inserted {b} at position {p + 1} as item {item_id}",
        )
        if result == "quota":
            return "failed"  # nothing was sent
        time.sleep(_VERIFY_WAIT_SECONDS)
        if new_id is None:
            return self._settle_errored_insert(action, base)
        b_item = (new_id, b)
        inserted = base.youtube[:p] + (b_item,) + base.youtube[p:]
        youtube = self._read_settled(
            action,
            "playlist_insert_verify",
            lambda: self._read_youtube(action.playlist_id),
            lambda got: got == inserted,
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
        self._record_check(action, "playlist_insert_verify", "YouTube side", inserted, youtube)
        if youtube != inserted:
            self._remove_b(action, base, new_id)
            return self._halt()
        j = action.b_index
        # YT Music shows B at j already; one more B there reads the same on either side of it.
        shown = base.ytmusic[:j] + (b,) + base.ytmusic[j:]
        ytmusic = self._side_after(
            action,
            "playlist_ytm_verify",
            shown,
            lambda: self._read_ytmusic(action.playlist_id),
            "YT Music side",
        )
        if ytmusic != shown:
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
        gone with B in its slot, then YT Music unchanged. While A's item may
        still be listed nothing is undone — the delete may yet land, and
        removing B then would leave neither. Once it is gone, a YT Music side
        that is off or unreadable puts A back (``_restore``)."""
        p = action.a_index
        replaced = base.youtube[:p] + (b_item,) + base.youtube[p + 1 :]
        time.sleep(_VERIFY_WAIT_SECONDS)
        youtube = self._youtube_after(action, "playlist_delete_verify", replaced)
        if youtube is None or action.a_playlist_item_id in {item_id for item_id, _ in youtube}:
            self.delete_unconfirmed.append(
                (action.playlist_title, p + 1, action.a_video_id, action.b_video_id)
            )
            return self._halt()
        # Known limit: a YT Music read served from before the insert would
        # pass too — as with the likes repoint's "LM unchanged" check.
        if youtube != replaced or (
            self._ytmusic_after(action, "playlist_check", base.ytmusic) != base.ytmusic
        ):
            return self._restore(action, base, b_item)
        # Both sides as planned, so later actions in this playlist build on it.
        self.expected[action.playlist_id] = _Sides(replaced, base.ytmusic)
        self.confirmed.add(action.playlist_id)
        return "applied" if sent_ok else "failed"

    def _restore(self, action: PlaylistAction, base: _Sides, b_item: tuple[str, str]) -> _Outcome:
        """Insert A again at its index and, once it is confirmed back there with
        B right after it and every other item unchanged, remove B. A comes back
        under a new item id. An A not confirmed back keeps B in its slot and is
        reported in ``left_removed``. Always stops the run."""
        p, a = action.a_index, action.a_video_id
        result, _ = self._write(
            action.item_id,
            "playlist_restore",
            lambda: self.yt.insert_playlist_item(action.playlist_id, a, p),
            lambda item_id: f"inserted {a} at position {p + 1} as item {item_id}",
        )
        if result != "quota":  # an errored insert may have landed too
            time.sleep(_VERIFY_WAIT_SECONDS)

            def wanted(got: _YouTube) -> _YouTube:
                """The baseline with A back at its index under the item id ``got``
                shows there (A's old one if none) and B right after it."""
                new_a = got[p][0] if p < len(got) and got[p][1] == a else action.a_playlist_item_id
                return base.youtube[:p] + ((new_a, a), b_item) + base.youtube[p + 1 :]

            def back(got: _YouTube) -> bool:
                return got == wanted(got) and got[p][0] != action.a_playlist_item_id

            youtube = self._read_settled(
                action,
                "playlist_restore_verify",
                lambda: self._read_youtube(action.playlist_id),
                back,
            )
            if youtube is not None:
                self._record_check(
                    action, "playlist_restore_verify", "YouTube side", wanted(youtube), youtube
                )
            if youtube is not None and back(youtube):
                restored = youtube[: p + 1] + youtube[p + 2 :]  # the baseline with A's new item id
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
        which is then removed. The run goes on only if B didn't land or landed
        at A's index and was cleanly removed."""
        p, b = action.a_index, action.b_video_id

        def at_p(got: _YouTube) -> bool:
            added = _added_item(base.youtube, got, b)
            return added is not None and got.index(added) == p

        # "Unchanged" is only concluded after the re-read: the insert may land late.
        youtube = self._read_settled(
            action,
            "playlist_insert_verify",
            lambda: self._read_youtube(action.playlist_id),
            at_p,
        )
        if youtube is None:
            self._report_inserted(action)
            return "failed"
        if youtube == base.youtube:
            self._record(
                action.item_id,
                "playlist_insert_verify",
                "failed",
                f"B isn't listed ({_RECHECKED}) — the insert call errored and didn't land",
            )
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
        if self._remove_b(action, base, added[0]) and youtube.index(added) != p:
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
            youtube = self._side_after(
                action,
                "playlist_rollback_verify",
                base.youtube,
                lambda: self._read_youtube(action.playlist_id),
                "YouTube side",
            )
            if youtube == base.youtube:
                return True
            if youtube is not None and b_item_id not in {item_id for item_id, _ in youtube}:
                self._halt()
                return False
        self._report_inserted(action)
        return False

    def _report_inserted(self, action: PlaylistAction) -> None:
        """B may be in the playlist next to A: report it and stop the run."""
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
        """Read a side until it equals ``expected`` (``_read_settled``) and
        record one ``kind`` row. Returns the last read, or None if it failed."""
        got = self._read_settled(action, kind, read, lambda got: got == expected)
        if got is not None:
            self._record_check(action, kind, side, expected, got)
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
        self,
        action: PlaylistAction,
        kind: str,
        side: str,
        expected: Sequence[object],
        got: Sequence[object],
    ) -> None:
        if got == expected:
            self._record(action.item_id, kind, "applied", f"{side} as expected")
        else:
            self._record(
                action.item_id,
                kind,
                "failed",
                f"{side}: {_difference(expected, got, 'expected')} ({_RECHECKED})",
            )

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
