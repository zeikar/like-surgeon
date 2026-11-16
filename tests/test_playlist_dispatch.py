"""Tests for the playlist repoint dispatcher (``likesurgeon.playlist_dispatch``).

It runs against ``FakePlaylistAccount`` (both clients at once): each
playlist's YouTube items plus how each video renders in YT Music, so every
insert and delete moves the YT Music side the way the rendering model says
(docs/design/playlist-repoint.md).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.db import make_session_factory
from likesurgeon.diagnosis import DIAGNOSIS_KIND_PLAYLISTS, ISSUE_PLAYLIST_RELINKED
from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt
from likesurgeon.playlist_dispatch import (
    MANUAL_SORT_REJECTED,
    PLAYLIST_STOP,
    PlaylistExecResult,
    _only_change,
    execute,
)
from likesurgeon.playlist_repoint import PlaylistAction, PlaylistState
from likesurgeon.sync_dispatch import QUOTA_REJECTED
from likesurgeon.youtube_client import YouTubeWriteError
from likesurgeon.ytmusic_client import UnexpectedResponseError

from .fake_playlists import FakePlaylistAccount


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real waits; the requested ones are recorded."""
    waits: list[float] = []
    monkeypatch.setattr("likesurgeon.playlist_dispatch.time.sleep", waits.append)
    return waits


def _diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(
        ytmusic_snapshot_id=None,
        youtube_snapshot_id=None,
        kind=DIAGNOSIS_KIND_PLAYLISTS,
        playlist_scan_id=None,
    )
    session.add(diag)
    session.flush()
    return diag


def _action(
    session: Session, diag: Diagnosis, acct: FakePlaylistAccount, playlist_id: str, a_index: int
) -> PlaylistAction:
    """The action for the item at ``a_index`` of the fake's current playlist."""
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type=ISSUE_PLAYLIST_RELINKED,
        confidence=1.0,
        reason="diagnosis-time evidence",
        status="open",
    )
    session.add(item)
    session.flush()
    items = acct.playlists[playlist_id]
    item_id, a = items[a_index]
    shown_before = [v for _, v in items[:a_index] if acct.renders.get(v, v) is not None]
    return PlaylistAction(
        item_id=item.id,
        playlist_id=playlist_id,
        playlist_title=f"T {playlist_id}",
        a_index=a_index,
        a_playlist_item_id=item_id,
        a_video_id=a,
        b_video_id=acct.renders[a],
        b_index=len(shown_before),
    )


def _canonical_action(
    session: Session,
    diag: Diagnosis,
    acct: FakePlaylistAccount,
    playlist_id: str,
    a_index: int,
    b: str,
    kind: Literal["canonical_repoint", "canonical_merge"] = "canonical_repoint",
) -> PlaylistAction:
    """The canonical action for the item at ``a_index``: A is shown as itself
    and is to be moved to ``b``; ``kind`` is the planner's forecast."""
    item = DiagnosisItem(
        diagnosis_id=diag.id,
        issue_type="playlist_canonical_version",
        confidence=1.0,
        reason="diagnosis-time evidence",
        status="open",
    )
    session.add(item)
    session.flush()
    items = acct.playlists[playlist_id]
    item_id, a = items[a_index]
    shown_before = [v for _, v in items[:a_index] if acct.renders.get(v, v) is not None]
    return PlaylistAction(
        item_id=item.id,
        playlist_id=playlist_id,
        playlist_title=f"T {playlist_id}",
        a_index=a_index,
        a_playlist_item_id=item_id,
        a_video_id=a,
        b_video_id=b,
        b_index=len(shown_before),
        kind=kind,
    )


def _states(acct: FakePlaylistAccount) -> dict[str, PlaylistState]:
    """Every playlist as it is now: what the planning scan saw."""
    return {
        pid: PlaylistState(youtube=tuple(items), ytmusic=tuple(acct.ytmusic(pid)))
        for pid, items in acct.playlists.items()
    }


def _steps(session: Session, item_id: int) -> list[tuple[str, str]]:
    return [(r.kind, r.status) for r in _attempts(session, item_id)]


def _attempts(session: Session, item_id: int) -> list[SyncAttempt]:
    return list(
        session.scalars(
            select(SyncAttempt)
            .where(SyncAttempt.diagnosis_item_id == item_id)
            .order_by(SyncAttempt.id)
        ).all()
    )


def _status(session: Session, action: PlaylistAction) -> str:
    item = session.get(DiagnosisItem, action.item_id)
    assert item is not None
    session.refresh(item)
    return item.status


_WRITES_AND_CHECKS = [
    ("playlist_insert", "applied"),
    ("playlist_insert_verify", "applied"),
    ("playlist_ytm_verify", "applied"),
    ("playlist_delete", "applied"),
    ("playlist_delete_verify", "applied"),
    ("playlist_check", "applied"),
]
_APPLIED = [("playlist_precheck", "applied"), *_WRITES_AND_CHECKS]
_ROLLBACK = [("playlist_rollback", "applied"), ("playlist_rollback_verify", "applied")]


def _one_playlist() -> FakePlaylistAccount:
    return FakePlaylistAccount(
        {"PL1": [("i0", "v0"), ("i1", "A"), ("i2", "v2")]}, renders={"A": "B"}
    )


# ---------------------------------------------------------------------------
# clean path
# ---------------------------------------------------------------------------


def test_repoint_puts_b_in_as_slot_and_leaves_ytmusic_unchanged(
    session: Session, sleeps: list[float]
) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert acct.write_calls == [("insert", "PL1", "B", "1"), ("delete", "i1")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "B"), ("i2", "v2")]
    assert acct.ytmusic("PL1") == list(states["PL1"].ytmusic)
    assert _steps(session, action.item_id) == _APPLIED
    assert _status(session, action) == "applied"
    assert sleeps == [5, 5]  # one wait after each write; no re-read


def test_two_actions_in_one_playlist_share_the_post_check_reads(session: Session) -> None:
    """A repoint keeps the list's length and every other slot, so the second
    action's indices still hold, and the first action's checks are its
    baseline. A hidden item makes YT Music's index differ from YouTube's."""
    acct = FakePlaylistAccount(
        {"PL1": [("i0", "D"), ("i1", "A1"), ("i2", "v2"), ("i3", "A2"), ("i4", "v4")]},
        renders={"D": None, "A1": "B1", "A2": "B2"},
    )
    diag = _diagnosis(session)
    first, second = _action(session, diag, acct, "PL1", 1), _action(session, diag, acct, "PL1", 3)
    assert (second.a_index, second.b_index) == (3, 2)
    states = _states(acct)
    session.commit()

    res = execute(session, [first, second], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=0, skipped=0)
    assert acct.playlists["PL1"] == [
        ("i0", "D"),
        ("new1", "B1"),
        ("i2", "v2"),
        ("new2", "B2"),
        ("i4", "v4"),
    ]
    assert acct.ytmusic("PL1") == list(states["PL1"].ytmusic)
    assert _steps(session, first.item_id) == _APPLIED
    assert _steps(session, second.item_id) == _WRITES_AND_CHECKS
    # One baseline read per side, then two checks per side per action.
    assert (acct.youtube_reads, acct.ytm_reads) == (5, 5)


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


def _two_playlists() -> FakePlaylistAccount:
    return FakePlaylistAccount(
        {
            "PL1": [("i0", "v0"), ("i1", "A1"), ("i2", "A2")],
            "PL2": [("j0", "w0"), ("j1", "A3")],
        },
        renders={"A1": "B1", "A2": "B2", "A3": "B3"},
    )


def _two_playlist_actions(session: Session, acct: FakePlaylistAccount) -> list[PlaylistAction]:
    diag = _diagnosis(session)
    return [
        _action(session, diag, acct, "PL1", 1),
        _action(session, diag, acct, "PL1", 2),
        _action(session, diag, acct, "PL2", 1),
    ]


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (lambda acct: acct.playlists["PL1"].insert(0, ("ix", "X")), "YouTube side"),
        (lambda acct: acct.ytm_reads_returning.update({1: ["v0", "B1"]}), "YT Music side"),
    ],
    ids=["youtube", "ytmusic"],
)
def test_a_playlist_changed_since_the_scan_gets_no_writes(
    session: Session, change, named: str
) -> None:
    acct = _two_playlists()
    actions = _two_playlist_actions(session, acct)
    states = _states(acct)
    session.commit()
    change(acct)

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=1, failed=1, skipped=0, unattempted=1, stale_playlists=("PL1",)
    )
    assert [c for c in acct.write_calls if c[0] == "insert"] == [("insert", "PL2", "B3", "1")]
    (row,) = _attempts(session, actions[0].item_id)
    assert (row.kind, row.status) == ("playlist_precheck", "failed")
    assert named in row.reason
    assert _attempts(session, actions[1].item_id) == []
    assert [_status(session, a) for a in actions] == ["open", "open", "applied"]


def test_precheck_reason_names_the_first_difference(session: Session) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.playlists["PL1"].insert(1, ("ix", "X"))

    execute(session, [action], states, ytm=acct, yt=acct)

    (row,) = _attempts(session, action.item_id)
    assert row.reason == (
        "YouTube side: 4 items (scan: 3), position 2 holds ('ix', 'X') (scan: ('i1', 'A')); "
        "YT Music side: 4 items (scan: 3), position 2 holds 'X' (scan: 'B')"
    )


def test_an_action_whose_a_or_b_is_not_where_it_says_gets_no_writes(session: Session) -> None:
    acct = _two_playlists()
    acct.playlists["PL1"][0] = ("i9", "D")
    acct.renders["D"] = None
    actions = _two_playlist_actions(session, acct)
    # The hidden D's slot (an off-by-one); YT Music does show B1 at index 0.
    actions[0] = replace(actions[0], a_index=0)
    states = _states(acct)
    session.commit()

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=1, failed=1, skipped=0, unattempted=1, stale_playlists=("PL1",)
    )
    assert [c for c in acct.write_calls if c[1] == "PL1"] == []
    (row,) = _attempts(session, actions[0].item_id)
    assert (row.kind, row.status) == ("playlist_precheck", "failed")
    assert row.reason == "YouTube side: position 1 holds ('i9', 'D'), not A ('i1', 'A1')"
    assert _status(session, actions[0]) == "open"


@pytest.mark.parametrize(
    ("knob", "error"),
    [
        ("ytm_read_errors", UnexpectedResponseError("shape changed")),
        ("youtube_read_errors", YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")),
    ],
    ids=["ytmusic", "youtube"],
)
def test_an_unreadable_baseline_stops_the_run(session: Session, knob: str, error) -> None:
    acct = _two_playlists()
    actions = _two_playlist_actions(session, acct)
    states = _states(acct)
    session.commit()
    getattr(acct, knob)[1] = error

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.aborted is not None and str(error) in res.aborted
    assert res == PlaylistExecResult(
        applied=0, failed=0, skipped=0, unattempted=3, aborted=res.aborted
    )
    assert acct.write_calls == []
    assert session.scalars(select(SyncAttempt)).all() == []
    assert [_status(session, a) for a in actions] == ["open"] * 3


def test_quota_on_the_baseline_read_stops_the_run(session: Session) -> None:
    acct = _two_playlists()
    actions = _two_playlist_actions(session, acct)
    states = _states(acct)
    session.commit()
    acct.quota_after = 0

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=0, skipped=0, unattempted=3, quota_exhausted=True
    )
    assert acct.write_calls == []
    assert session.scalars(select(SyncAttempt)).all() == []


def test_a_check_read_error_is_recorded_and_stops_the_run(session: Session) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.youtube_read_errors[2] = YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")

    res = execute(session, [action], states, ytm=acct, yt=acct)

    rows = _attempts(session, action.item_id)
    assert (rows[2].kind, rows[2].status) == ("playlist_insert_verify", "failed")
    assert rows[2].reason == f"read failed: YouTube: {acct.youtube_read_errors[2]}"
    assert [(r.kind, r.status) for r in rows[3:]] == _ROLLBACK  # B is removed again
    assert res.aborted is not None and "HTTP 500: boom" in res.aborted


def test_quota_on_a_check_read_names_the_quota_not_a_failed_repoint(session: Session) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.quota_after = 2  # baseline read and insert; the insert's check read is refused

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert (res.quota_exhausted, res.aborted) == (True, None)
    rows = _attempts(session, action.item_id)
    assert (rows[2].kind, rows[2].status) == ("playlist_insert_verify", "failed")
    assert "quotaExceeded" in rows[2].reason
    # Removing B is refused too, so B is reported.
    assert (rows[3].kind, rows[3].status) == ("playlist_rollback", "failed")
    assert res.left_inserted == (("T PL1", 2, "B"),)


# ---------------------------------------------------------------------------
# insert failures and removing B again
# ---------------------------------------------------------------------------


def _prepared(session: Session) -> tuple[FakePlaylistAccount, list[PlaylistAction], dict]:
    """Two playlists, three actions (two in PL1), as scanned."""
    acct = _two_playlists()
    actions = _two_playlist_actions(session, acct)
    states = _states(acct)
    session.commit()
    return acct, actions, states


def test_an_insert_the_quota_rejects_stops_the_run(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.quota_after = 1  # the baseline read

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=1, skipped=0, unattempted=2, quota_exhausted=True
    )
    rows = _attempts(session, actions[0].item_id)
    assert [(r.kind, r.status) for r in rows] == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "failed"),
    ]
    assert rows[-1].reason.startswith(QUOTA_REJECTED)
    assert acct.write_calls == []
    assert (acct.youtube_reads, acct.ytm_reads) == (1, 1)


def test_an_accepted_insert_that_is_not_listed_is_reported_and_stops_the_run(
    session: Session, sleeps: list[float]
) -> None:
    """A 2xx insert may still land later, so B is reported as possibly there."""
    acct, actions, states = _prepared(session)
    acct.insert_does_not_land = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0,
        failed=1,
        skipped=0,
        unattempted=2,
        aborted=PLAYLIST_STOP,
        left_inserted=(("T PL1", 2, "B1"),),
    )
    assert _steps(session, actions[0].item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "applied"),
        ("playlist_insert_verify", "failed"),
    ]
    assert _status(session, actions[0]) == "open"
    assert acct.write_calls == [("insert", "PL1", "B1", "1")]
    assert sleeps == [5, 15]


def test_an_insert_that_errored_and_is_not_listed_is_reported_and_stops(
    session: Session, sleeps: list[float]
) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_errors = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.applied == 0 and res.failed == 1
    assert res.left_inserted == (("T PL1", 2, "B1"),)  # a timeout isn't a rejection
    assert res.aborted == PLAYLIST_STOP
    assert sleeps[:2] == [5, 15]  # "unchanged" only after the re-read
    assert _steps(session, actions[0].item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "failed"),
        ("playlist_insert_verify", "failed"),
    ]
    assert _status(session, actions[0]) == "open"
    assert ("i1", "A1") in acct.playlists["PL1"]


def test_an_insert_that_errored_but_landed_is_removed_again(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_errors_after_landing = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=1, skipped=0)
    assert _steps(session, actions[0].item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "failed"),
        ("playlist_insert_verify", "failed"),
        *_ROLLBACK,
    ]
    # Only the item this action added is deleted, by its item id.
    assert acct.write_calls[:3] == [
        ("insert", "PL1", "B1", "1"),
        ("delete", "new1"),
        ("insert", "PL1", "B2", "2"),
    ]
    assert _status(session, actions[0]) == "open"
    # B1 gone again; the next action then repointed A2.
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i1", "A1"), ("new2", "B2")]


def test_an_errored_insert_at_another_index_is_removed_and_stops_the_run(
    session: Session,
) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_errors_after_landing = {"B1"}
    acct.insert_lands_at_end = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=1, skipped=0, unattempted=2, aborted=PLAYLIST_STOP
    )
    assert _steps(session, actions[0].item_id)[-3:] == [
        ("playlist_insert_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.playlists["PL1"] == list(states["PL1"].youtube)


def test_an_errored_insert_whose_item_cannot_be_identified_deletes_nothing(
    session: Session,
) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_errors_after_landing = {"B1"}
    insert = acct.insert_playlist_item

    def insert_and_someone_adds_another(playlist_id: str, video_id: str, position: int) -> str:
        acct.playlists[playlist_id].append(("ix", "B1"))
        return insert(playlist_id, video_id, position)

    acct.insert_playlist_item = insert_and_someone_adds_another  # type: ignore[method-assign]

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0,
        failed=1,
        skipped=0,
        unattempted=2,
        aborted=PLAYLIST_STOP,
        left_inserted=(("T PL1", 2, "B1"),),
    )
    assert [c for c in acct.write_calls if c[0] == "delete"] == []
    row = _attempts(session, actions[0].item_id)[-1]
    assert (row.kind, row.status) == ("playlist_insert_verify", "failed")
    assert "can't tell which item" in row.reason


def test_an_insert_at_another_index_is_removed_and_stops_the_run(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_lands_at_end = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=1, skipped=0, unattempted=2, aborted=PLAYLIST_STOP
    )
    assert _steps(session, actions[0].item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "applied"),
        ("playlist_insert_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.playlists["PL1"] == list(states["PL1"].youtube)
    assert acct.write_calls == [("insert", "PL1", "B1", "1"), ("delete", "new1")]


def test_b_not_shown_by_ytmusic_is_removed_and_a_kept(
    session: Session, sleeps: list[float]
) -> None:
    acct, actions, states = _prepared(session)
    unchanged = list(states["PL1"].ytmusic)
    acct.ytm_reads_returning = {2: unchanged, 3: unchanged}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=0, skipped=1, unattempted=2, aborted=PLAYLIST_STOP
    )
    assert _steps(session, actions[0].item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_insert", "applied"),
        ("playlist_insert_verify", "applied"),
        ("playlist_ytm_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.write_calls == [("insert", "PL1", "B1", "1"), ("delete", "new1")]
    assert acct.playlists["PL1"] == list(states["PL1"].youtube)
    assert _status(session, actions[0]) == "skipped"
    assert sleeps == [5, 15, 5]


def test_an_unreadable_ytmusic_check_removes_b_and_stops_the_run(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.ytm_read_errors = {2: UnexpectedResponseError("shape changed")}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.aborted is not None and "shape changed" in res.aborted
    assert (res.failed, res.unattempted) == (1, 2)
    assert _steps(session, actions[0].item_id)[-3:] == [
        ("playlist_ytm_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.write_calls == [("insert", "PL1", "B1", "1"), ("delete", "new1")]
    assert _status(session, actions[0]) == "open"


def test_a_stale_ytmusic_read_is_settled_by_the_re_read(
    session: Session, sleeps: list[float]
) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.ytm_stale_reads = {2}  # repeats the baseline read

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert _steps(session, action.item_id) == _APPLIED
    assert sleeps == [5, 15, 5]


@pytest.mark.parametrize("knob", ["delete_errors", "delete_does_not_land"])
def test_a_b_that_cannot_be_removed_is_reported(session: Session, knob: str) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_lands_at_end = {"B1"}
    getattr(acct, knob).add("new1")

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.left_inserted == (("T PL1", 2, "B1"),)
    assert res.aborted == PLAYLIST_STOP
    assert res.unattempted == 2
    assert _attempts(session, actions[0].item_id)[-1].status == "failed"


def test_a_failed_insert_after_an_applied_repoint_does_not_make_the_playlist_stale(
    session: Session,
) -> None:
    acct = FakePlaylistAccount(
        {"PL1": [("i1", "A1"), ("i2", "A2"), ("i3", "A3")]},
        renders={"A1": "B1", "A2": "B2", "A3": "B3"},
    )
    diag = _diagnosis(session)
    actions = [_action(session, diag, acct, "PL1", i) for i in range(3)]
    states = _states(acct)
    session.commit()
    acct.insert_errors_after_landing = {"B2"}  # lands at A's index, removed: the run goes on

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=1, skipped=0)
    assert [v for _, v in acct.playlists["PL1"]] == ["B1", "A2", "B3"]
    # The third action re-reads both sides and compares them with the scan
    # plus the first repoint.
    assert _steps(session, actions[2].item_id) == _APPLIED


def test_a_b_removal_that_cannot_be_read_back_is_reported(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_lands_at_end = {"B1"}
    # Reads: baseline, insert check and its re-read, then the removal's check.
    acct.youtube_read_errors[4] = YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.left_inserted == (("T PL1", 2, "B1"),)
    assert res.aborted is not None and "HTTP 500: boom" in res.aborted
    assert _steps(session, actions[0].item_id)[-2:] == [
        ("playlist_rollback", "applied"),
        ("playlist_rollback_verify", "failed"),
    ]


def test_a_removed_b_with_another_change_stops_without_reporting_b(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.insert_lands_at_end = {"B1"}
    delete = acct.delete_playlist_item

    def delete_and_someone_adds(item_id: str) -> None:
        delete(item_id)
        acct.playlists["PL1"].append(("iy", "Y"))

    acct.delete_playlist_item = delete_and_someone_adds  # type: ignore[method-assign]

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert (res.left_inserted, res.aborted) == ((), PLAYLIST_STOP)
    row = _attempts(session, actions[0].item_id)[-1]
    assert (row.kind, row.status) == ("playlist_rollback_verify", "failed")


# ---------------------------------------------------------------------------
# delete failures, restore, interruption
# ---------------------------------------------------------------------------

_INSERTED = [
    ("playlist_precheck", "applied"),
    ("playlist_insert", "applied"),
    ("playlist_insert_verify", "applied"),
    ("playlist_ytm_verify", "applied"),
]
_RESTORE = [("playlist_restore", "applied"), ("playlist_restore_verify", "applied")]
_B_AT_A = [("i0", "v0"), ("new1", "B1"), ("i2", "A2")]  # PL1 after the first repoint


def test_a_delete_the_quota_rejects_tries_to_remove_b(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.quota_after = 3  # baseline read, insert, its check read

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0,
        failed=1,
        skipped=0,
        unattempted=2,
        quota_exhausted=True,
        left_inserted=(("T PL1", 2, "B1"),),
    )
    rows = _attempts(session, actions[0].item_id)
    assert [(r.kind, r.status) for r in rows] == [
        *_INSERTED,
        ("playlist_delete", "failed"),
        ("playlist_rollback", "failed"),
    ]
    assert rows[-2].reason.startswith(QUOTA_REJECTED)
    assert _status(session, actions[0]) == "open"


@pytest.mark.parametrize(
    "knob",
    [
        lambda acct: acct.delete_does_not_land.add("i1"),
        lambda acct: acct.delete_errors.add("i1"),
        lambda acct: acct.youtube_read_errors.update(
            {3: YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")}
        ),
    ],
    ids=["accepted", "errored", "unreadable"],
)
def test_a_delete_youtube_cannot_confirm_is_reported_and_nothing_else_is_written(
    session: Session, knob
) -> None:
    acct, actions, states = _prepared(session)
    knob(acct)

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.delete_unconfirmed == (("T PL1", 2, "A1", "B1"),)
    assert res.aborted is not None
    assert (res.applied, res.failed, res.unattempted) == (0, 1, 2)
    assert acct.write_calls == [("insert", "PL1", "B1", "1"), ("delete", "i1")]
    assert acct.playlists["PL1"][1] == ("new1", "B1")
    assert _steps(session, actions[0].item_id)[-1] == ("playlist_delete_verify", "failed")
    assert _status(session, actions[0]) == "open"


def test_a_delete_landing_after_the_checks_leaves_b_in_as_slot(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.delete_lands_late = {"i1": 2}  # right after the delete check's re-read

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.delete_unconfirmed == (("T PL1", 2, "A1", "B1"),)
    assert acct.write_calls == [("insert", "PL1", "B1", "1"), ("delete", "i1")]
    assert acct.playlists["PL1"] == _B_AT_A  # B in A's slot, never neither


def test_a_lagging_youtube_read_after_the_delete_is_settled_by_the_re_read(
    session: Session, sleeps: list[float]
) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.delete_read_lag = {"i1": 1}  # YT Music already matches

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert _steps(session, action.item_id) == _APPLIED
    assert sleeps == [5, 5, 15]


@pytest.mark.parametrize(
    "knob",
    [
        lambda acct: acct.ytm_reads_returning.update({3: ["v0", "A2"], 4: ["v0", "A2"]}),
        lambda acct: acct.ytm_read_errors.update({3: UnexpectedResponseError("shape changed")}),
    ],
    ids=["differs", "unreadable"],
)
def test_ytmusic_off_after_the_delete_puts_a_back_then_removes_b(session: Session, knob) -> None:
    acct, actions, states = _prepared(session)
    knob(acct)

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert (res.applied, res.failed, res.skipped, res.restored) == (0, 0, 1, 1)
    assert (res.unattempted, res.aborted is not None) == (2, True)
    assert acct.write_calls == [
        ("insert", "PL1", "B1", "1"),
        ("delete", "i1"),
        ("insert", "PL1", "A1", "1"),
        ("delete", "new1"),
    ]
    assert [v for _, v in acct.playlists["PL1"]] == [v for _, v in states["PL1"].youtube]
    assert acct.playlists["PL1"][1] == ("new2", "A1")  # A is back under a new item id
    assert _steps(session, actions[0].item_id)[-6:] == [
        ("playlist_delete_verify", "applied"),
        ("playlist_check", "failed"),
        *_RESTORE,
        *_ROLLBACK,
    ]
    assert _status(session, actions[0]) == "skipped"


def test_ytmusic_differing_after_the_delete_stops_with_playlist_stop(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.ytm_reads_returning = {3: ["v0", "A2"], 4: ["v0", "A2"]}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.aborted == PLAYLIST_STOP


def test_an_a_that_cannot_be_put_back_keeps_b_and_is_reported(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.ytm_reads_returning = {3: ["v0", "A2"], 4: ["v0", "A2"]}
    acct.insert_errors = {"A1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.left_removed == (("T PL1", 2, "A1"),)
    assert (res.restored, res.aborted) == (0, PLAYLIST_STOP)
    assert ("delete", "new1") not in acct.write_calls
    assert acct.playlists["PL1"] == _B_AT_A
    assert _status(session, actions[0]) == "open"


def test_an_a_put_back_elsewhere_keeps_b_and_is_reported(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.ytm_reads_returning = {3: ["v0", "A2"], 4: ["v0", "A2"]}
    acct.insert_lands_at_end = {"A1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.left_removed == (("T PL1", 2, "A1"),)
    assert ("delete", "new1") not in acct.write_calls
    assert _steps(session, actions[0].item_id)[-1] == ("playlist_restore_verify", "failed")


def test_a_restore_with_another_slot_changed_keeps_b_and_is_reported(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.ytm_reads_returning = {3: ["v0", "A2"], 4: ["v0", "A2"]}
    insert = acct.insert_playlist_item

    def insert_and_someone_re_adds_v0(playlist_id: str, video_id: str, position: int) -> str:
        # Same video ids, but slot 1 is now a different item: only item ids show it.
        if video_id == "A1":
            acct.playlists[playlist_id][0] = ("i0x", "v0")
        return insert(playlist_id, video_id, position)

    acct.insert_playlist_item = insert_and_someone_re_adds_v0  # type: ignore[method-assign]

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res.left_removed == (("T PL1", 2, "A1"),)
    assert ("delete", "new1") not in acct.write_calls
    row = _attempts(session, actions[0].item_id)[-1]
    assert (row.kind, row.status) == ("playlist_restore_verify", "failed")
    assert "position 1 holds ('i0x', 'v0')" in row.reason


def test_a_delete_that_errored_but_landed_stays_open_and_the_run_goes_on(
    session: Session,
) -> None:
    acct, actions, states = _prepared(session)
    acct.delete_errors_after_landing = {"i1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=1, skipped=0)
    assert _steps(session, actions[0].item_id) == [
        *_INSERTED,
        ("playlist_delete", "failed"),
        ("playlist_delete_verify", "applied"),
        ("playlist_check", "applied"),
    ]
    assert _status(session, actions[0]) == "open"
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "B1"), ("new2", "B2")]


def test_an_interrupt_after_the_delete_call_is_recorded(session: Session) -> None:
    factory = make_session_factory(session.bind)
    acct, actions, states = _prepared(session)
    acct.youtube_read_errors[3] = KeyboardInterrupt()  # the delete's check read

    with pytest.raises(KeyboardInterrupt):
        execute(session, actions, states, ytm=acct, yt=acct)

    fresh = factory()
    try:
        assert _steps(fresh, actions[0].item_id) == [
            *_INSERTED,
            ("playlist_delete", "applied"),
            ("playlist_interrupted", "failed"),
        ]
    finally:
        fresh.close()


def test_each_write_row_is_on_disk_before_the_next_wait(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(session.bind)
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    committed_at_waits: list[list[tuple[str, str]]] = []

    def sleep(_seconds: float) -> None:
        fresh = factory()
        try:
            committed_at_waits.append(_steps(fresh, action.item_id))
        finally:
            fresh.close()

    monkeypatch.setattr("likesurgeon.playlist_dispatch.time.sleep", sleep)

    execute(session, [action], states, ytm=acct, yt=acct)

    assert committed_at_waits[0][-1] == ("playlist_insert", "applied")
    assert committed_at_waits[1][-1] == ("playlist_delete", "applied")


# ---------------------------------------------------------------------------
# automatically sorted playlists
# ---------------------------------------------------------------------------

_SORTED = ("T PL1",)


@pytest.mark.parametrize("at", [None, 1], ids=["sorted", "positional"])
def test_only_change_rejects_any_other_change(at: int | None) -> None:
    rest = ("a", "b", "c")
    assert _only_change(("a", "x", "b", "c"), rest, "x", at) == 1
    assert _only_change(("b", "x", "a", "c"), rest, "x", at) is None  # another item moved
    assert _only_change(("a", "x", "b"), rest, "x", at) is None  # another item gone
    assert _only_change(("a", "x", "b", "c", "d"), rest, "x", at) is None  # another added
    assert _only_change(("a", "y", "b", "c"), rest, "x", at) is None  # not x
    assert _only_change(rest, rest, "x", at) is None  # x missing


def test_only_change_holds_x_where_a_positional_playlist_wants_it() -> None:
    rest = ("a", "b", "c")
    assert _only_change(("a", "b", "x", "c"), rest, "x") == 2
    assert _only_change(("a", "b", "x", "c"), rest, "x", 1) is None
    # One more of an x that is already there (YT Music's B), on either side of it.
    assert _only_change(("a", "b", "b", "c"), rest, "b", 1) == 1
    assert _only_change(("a", "b", "c", "b"), rest, "b", 1) is None
    assert _only_change(("a", "b", "c", "b"), rest, "b") == 3


def _sorted_playlist(rule: str) -> FakePlaylistAccount:
    acct = FakePlaylistAccount(
        {"PL1": [("i0", "v0"), ("i1", "A1"), ("i2", "v2")]}, renders={"A1": "B1"}
    )
    acct.auto_sorted = {"PL1": rule}
    return acct


_REFUSED_THEN_INSERTED = [
    ("playlist_precheck", "applied"),
    ("playlist_insert", "failed"),
    ("playlist_insert", "applied"),
]


@pytest.mark.parametrize(
    ("rule", "after"),
    [
        ("top", [("new1", "B1"), ("i0", "v0"), ("i2", "v2")]),
        ("end", [("i0", "v0"), ("i2", "v2"), ("new1", "B1")]),
    ],
)
def test_a_sorted_playlist_refusing_the_position_gets_b_where_its_order_puts_it(
    session: Session, rule: str, after: list
) -> None:
    acct = _sorted_playlist(rule)
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=1, failed=0, skipped=0, reordered=1, sorted_playlists=_SORTED
    )
    assert acct.write_calls == [
        ("insert", "PL1", "B1", "1"),  # refused: the playlist is sorted automatically
        ("insert", "PL1", "B1"),
        ("delete", "i1"),
    ]
    assert acct.playlists["PL1"] == after
    rows = _attempts(session, action.item_id)
    assert [(r.kind, r.status) for r in rows] == [*_REFUSED_THEN_INSERTED, *_WRITES_AND_CHECKS[1:]]
    assert rows[1].reason.startswith(MANUAL_SORT_REJECTED)
    assert "manual sorting" in rows[1].reason
    assert "sort order" in rows[2].reason
    assert _status(session, action) == "applied"


@pytest.mark.parametrize(
    ("rule", "after"),
    [
        ("top", ["B2", "B1", "D", "v2", "v4"]),
        # Oldest first: B1 lands after A2, so A2's and B2's indices drop by one.
        ("end", ["D", "v2", "v4", "B1", "B2"]),
    ],
)
def test_a_later_action_in_a_sorted_playlist_inserts_without_a_position(
    session: Session, rule: str, after: list[str]
) -> None:
    acct = FakePlaylistAccount(
        {"PL1": [("i0", "D"), ("i1", "A1"), ("i2", "v2"), ("i3", "A2"), ("i4", "v4")]},
        renders={"D": None, "A1": "B1", "A2": "B2"},
    )
    acct.auto_sorted = {"PL1": rule}
    diag = _diagnosis(session)
    first, second = _action(session, diag, acct, "PL1", 1), _action(session, diag, acct, "PL1", 3)
    states = _states(acct)
    session.commit()

    res = execute(session, [first, second], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=2, failed=0, skipped=0, reordered=2, sorted_playlists=_SORTED
    )
    assert acct.write_calls == [
        ("insert", "PL1", "B1", "1"),
        ("insert", "PL1", "B1"),
        ("delete", "i1"),
        ("insert", "PL1", "B2"),  # straight without a position
        ("delete", "i3"),
    ]
    assert [v for _, v in acct.playlists["PL1"]] == after
    assert acct.ytmusic("PL1") == [v for v in after if v != "D"]
    assert _steps(session, second.item_id) == _WRITES_AND_CHECKS
    assert (acct.youtube_reads, acct.ytm_reads) == (5, 5)


def test_a_sorted_playlist_and_a_positional_one_in_the_same_run(session: Session) -> None:
    acct, actions, states = _prepared(session)
    acct.auto_sorted = {"PL2": "top"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=3, failed=0, skipped=0, reordered=1, sorted_playlists=("T PL2",)
    )
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "B1"), ("new2", "B2")]
    assert [v for _, v in acct.playlists["PL2"]] == ["B3", "w0"]


def test_b_not_shown_by_ytmusic_in_a_sorted_playlist_is_removed(session: Session) -> None:
    acct = _sorted_playlist("end")
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    unchanged = list(states["PL1"].ytmusic)
    acct.ytm_reads_returning = {2: unchanged, 3: unchanged}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=0, failed=0, skipped=1, aborted=PLAYLIST_STOP, sorted_playlists=_SORTED
    )
    assert _steps(session, action.item_id) == [
        *_REFUSED_THEN_INSERTED,
        ("playlist_insert_verify", "applied"),
        ("playlist_ytm_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.playlists["PL1"] == list(states["PL1"].youtube)


def test_another_item_moved_in_a_sorted_playlist_fails_the_check(session: Session) -> None:
    acct = _sorted_playlist("end")
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    insert = acct.insert_playlist_item

    def insert_and_the_sort_moves_another(
        playlist_id: str, video_id: str, position: int | None = None
    ) -> str:
        item_id = insert(playlist_id, video_id, position)
        items = acct.playlists[playlist_id]
        items[0], items[2] = items[2], items[0]
        return item_id

    acct.insert_playlist_item = insert_and_the_sort_moves_another  # type: ignore[method-assign]

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.aborted == PLAYLIST_STOP
    assert ("delete", "i1") not in acct.write_calls  # A is never deleted
    assert ("delete", "new1") in acct.write_calls  # B removed again
    rows = _attempts(session, action.item_id)
    assert (rows[3].kind, rows[3].status) == ("playlist_insert_verify", "failed")
    assert "position 1 holds ('i2', 'v2')" in rows[3].reason


def test_an_errored_insert_in_a_sorted_playlist_landing_anywhere_is_removed_and_the_run_goes_on(
    session: Session,
) -> None:
    acct, actions, states = _prepared(session)
    acct.auto_sorted = {"PL1": "end"}
    acct.insert_errors_after_landing = {"B1"}

    res = execute(session, actions, states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=2, failed=1, skipped=0, reordered=1, sorted_playlists=_SORTED
    )
    assert _steps(session, actions[0].item_id) == [
        *_REFUSED_THEN_INSERTED[:2],
        ("playlist_insert", "failed"),  # landed, then errored
        ("playlist_insert_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.write_calls[:3] == [
        ("insert", "PL1", "B1", "1"),
        ("insert", "PL1", "B1"),
        ("delete", "new1"),
    ]
    assert [v for _, v in acct.playlists["PL1"]] == ["v0", "A1", "B2"]


@pytest.mark.parametrize(
    "knob",
    [
        lambda acct: acct.delete_does_not_land.add("i1"),
        lambda acct: acct.youtube_read_errors.update(
            {3: YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")}
        ),
    ],
    ids=["accepted", "unreadable"],
)
def test_an_unconfirmed_delete_in_a_sorted_playlist_undoes_nothing(session: Session, knob) -> None:
    acct = _sorted_playlist("end")
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    knob(acct)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.delete_unconfirmed == (("T PL1", 2, "A1", "B1"),)
    assert res.aborted is not None
    assert acct.write_calls == [
        ("insert", "PL1", "B1", "1"),
        ("insert", "PL1", "B1"),
        ("delete", "i1"),
    ]
    assert ("new1", "B1") in acct.playlists["PL1"]
    assert _steps(session, action.item_id)[-1] == ("playlist_delete_verify", "failed")


def test_ytmusic_off_after_the_delete_in_a_sorted_playlist_puts_a_back_where_it_sorts(
    session: Session,
) -> None:
    acct = _sorted_playlist("end")
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.ytm_reads_returning = {3: ["v0", "v2"], 4: ["v0", "v2"]}  # the check and its re-read

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert (res.skipped, res.restored, res.reordered, res.aborted) == (1, 1, 0, PLAYLIST_STOP)
    assert acct.write_calls == [
        ("insert", "PL1", "B1", "1"),
        ("insert", "PL1", "B1"),
        ("delete", "i1"),
        ("insert", "PL1", "A1"),  # straight without a position
        ("delete", "new1"),  # B removed once A is confirmed back
    ]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("new2", "A1")]
    assert _steps(session, action.item_id)[-6:] == [
        ("playlist_delete_verify", "applied"),
        ("playlist_check", "failed"),
        *_RESTORE,
        *_ROLLBACK,
    ]
    assert _status(session, action) == "skipped"


def test_an_a_that_cannot_be_put_back_in_a_sorted_playlist_keeps_b(session: Session) -> None:
    acct = _sorted_playlist("end")
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.ytm_reads_returning = {3: ["v0", "v2"], 4: ["v0", "v2"]}
    acct.insert_errors = {"A1"}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.left_removed == (("T PL1", 2, "A1"),)
    assert ("delete", "new1") not in acct.write_calls
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("new1", "B1")]


# ---------------------------------------------------------------------------
# canonical actions: A shown as itself, moved to the release B the artist lists
# ---------------------------------------------------------------------------

_MERGED = [
    ("playlist_precheck", "applied"),
    ("playlist_delete", "applied"),
    ("playlist_delete_verify", "applied"),
    ("playlist_check", "applied"),
]


def _plain_playlist() -> FakePlaylistAccount:
    """A shown as itself; B not in the playlist."""
    return FakePlaylistAccount({"PL1": [("i0", "v0"), ("i1", "A"), ("i2", "v2")]})


def _with_b() -> FakePlaylistAccount:
    """A shown as itself; B already in the playlist (both sides)."""
    return FakePlaylistAccount({"PL1": [("i0", "v0"), ("i1", "A"), ("i2", "v2"), ("i3", "B")]})


def _canonical(
    session: Session,
    acct: FakePlaylistAccount,
    kind: Literal["canonical_repoint", "canonical_merge"] = "canonical_repoint",
) -> tuple[PlaylistAction, dict[str, PlaylistState]]:
    """PL1's A at index 1 moved to B, as scanned."""
    action = _canonical_action(session, _diagnosis(session), acct, "PL1", 1, "B", kind)
    states = _states(acct)
    session.commit()
    return action, states


def test_a_canonical_repoint_puts_b_in_as_slot_and_ytmusic_shows_b_where_a_was(
    session: Session,
) -> None:
    acct = _plain_playlist()
    action, states = _canonical(session, acct)
    assert states["PL1"].ytmusic == ("v0", "A", "v2")

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert acct.write_calls == [("insert", "PL1", "B", "1"), ("delete", "i1")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "B"), ("i2", "v2")]
    assert acct.ytmusic("PL1") == ["v0", "B", "v2"]
    assert _steps(session, action.item_id) == _APPLIED
    assert _status(session, action) == "applied"


def test_a_canonical_repoint_in_a_sorted_playlist_puts_b_where_its_order_says(
    session: Session,
) -> None:
    acct = _plain_playlist()
    acct.auto_sorted = {"PL1": "end"}
    action, states = _canonical(session, acct)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(
        applied=1, failed=0, skipped=0, reordered=1, sorted_playlists=_SORTED
    )
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("new1", "B")]
    assert acct.ytmusic("PL1") == ["v0", "v2", "B"]
    assert _steps(session, action.item_id) == [*_REFUSED_THEN_INSERTED, *_WRITES_AND_CHECKS[1:]]


def test_a_canonical_b_not_shown_by_ytmusic_is_removed_and_a_kept(session: Session) -> None:
    acct = _plain_playlist()
    action, states = _canonical(session, acct)
    unchanged = list(states["PL1"].ytmusic)
    acct.ytm_reads_returning = {2: unchanged, 3: unchanged}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=0, failed=0, skipped=1, aborted=PLAYLIST_STOP)
    assert _steps(session, action.item_id) == [
        *_INSERTED[:3],
        ("playlist_ytm_verify", "failed"),
        *_ROLLBACK,
    ]
    assert acct.write_calls == [("insert", "PL1", "B", "1"), ("delete", "new1")]
    assert acct.playlists["PL1"] == list(states["PL1"].youtube)
    assert _status(session, action) == "skipped"


def test_a_canonical_action_whose_ytmusic_index_shows_another_video_gets_no_writes(
    session: Session,
) -> None:
    acct = _plain_playlist()
    action, states = _canonical(session, acct)
    action = replace(action, b_index=0)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=0, failed=1, skipped=0, stale_playlists=("PL1",))
    assert acct.write_calls == []
    (row,) = _attempts(session, action.item_id)
    assert (row.kind, row.status) == ("playlist_precheck", "failed")
    assert row.reason == "YT Music side: position 1 holds 'v0', not A 'A'"


@pytest.mark.parametrize(
    ("third", "renders", "named"),
    [
        ("D", {"D": "B"}, "B 'B' is shown by YT Music but not listed on the YouTube side"),
        ("B", {"B": None}, "B 'B' is listed on the YouTube side but not shown by YT Music"),
    ],
    ids=["ytmusic-only", "youtube-only"],
)
def test_a_canonical_action_whose_b_is_on_one_side_only_gets_no_writes(
    session: Session, third: str, renders: dict[str, str | None], named: str
) -> None:
    """B shown as the rendering of another item (or listed but hidden): what
    a check of B would see doesn't stand for B's own item."""
    acct = FakePlaylistAccount({"PL1": [("i0", "v0"), ("i1", "A"), ("i2", third)]}, renders=renders)
    action, states = _canonical(session, acct)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=0, failed=1, skipped=0, stale_playlists=("PL1",))
    assert acct.write_calls == []
    (row,) = _attempts(session, action.item_id)
    assert (row.kind, row.status) == ("playlist_precheck", "failed")
    assert row.reason == named
    assert _status(session, action) == "open"


def test_a_merge_deletes_a_only(session: Session, sleeps: list[float]) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert acct.write_calls == [("delete", "i1")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("i3", "B")]
    assert acct.ytmusic("PL1") == ["v0", "v2", "B"]
    assert _steps(session, action.item_id) == _MERGED
    assert _status(session, action) == "applied"
    assert sleeps == [5]


@pytest.mark.parametrize(
    ("make", "forecast", "steps", "writes"),
    [
        (
            _plain_playlist,
            "canonical_merge",
            _APPLIED,
            [("insert", "PL1", "B", "1"), ("delete", "i1")],
        ),
        (_with_b, "canonical_repoint", _MERGED, [("delete", "i1")]),
    ],
    ids=["forecast-merge-b-absent", "forecast-repoint-b-present"],
)
def test_the_flow_follows_the_baseline_not_the_forecast(
    session: Session, make, forecast, steps: list, writes: list
) -> None:
    acct = make()
    action, states = _canonical(session, acct, forecast)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=0, skipped=0)
    assert acct.write_calls == writes
    assert _steps(session, action.item_id) == steps


def test_two_as_sharing_one_b_run_repoint_then_merge_on_one_baseline(session: Session) -> None:
    acct = FakePlaylistAccount({"PL1": [("i0", "v0"), ("i1", "A1"), ("i2", "v2"), ("i3", "A2")]})
    diag = _diagnosis(session)
    first = _canonical_action(session, diag, acct, "PL1", 1, "B")
    second = _canonical_action(session, diag, acct, "PL1", 3, "B", "canonical_merge")
    states = _states(acct)
    session.commit()

    res = execute(session, [first, second], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=0, skipped=0)
    assert acct.write_calls == [("insert", "PL1", "B", "1"), ("delete", "i1"), ("delete", "i3")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "B"), ("i2", "v2")]
    assert _steps(session, first.item_id) == _APPLIED
    assert _steps(session, second.item_id) == _MERGED[1:]
    # One baseline read per side; two checks per side for the repoint, one for the merge.
    assert (acct.youtube_reads, acct.ytm_reads) == (4, 4)


def test_a_later_action_after_a_merge_finds_its_a_one_slot_up(session: Session) -> None:
    acct = FakePlaylistAccount(
        {"PL1": [("i0", "A1"), ("i1", "B1"), ("i2", "v2"), ("i3", "A2")]},
    )
    diag = _diagnosis(session)
    first = _canonical_action(session, diag, acct, "PL1", 0, "B1", "canonical_merge")
    second = _canonical_action(session, diag, acct, "PL1", 3, "B2")
    states = _states(acct)
    session.commit()

    res = execute(session, [first, second], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=2, failed=0, skipped=0)
    assert acct.write_calls == [("delete", "i0"), ("insert", "PL1", "B2", "2"), ("delete", "i3")]
    assert acct.playlists["PL1"] == [("i1", "B1"), ("i2", "v2"), ("new1", "B2")]
    assert _steps(session, second.item_id) == _WRITES_AND_CHECKS


# ---------------------------------------------------------------------------
# merge failures
# ---------------------------------------------------------------------------


def test_a_merge_delete_the_quota_rejects_sends_nothing(session: Session) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    acct.quota_after = 1  # the baseline read

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True)
    rows = _attempts(session, action.item_id)
    assert [(r.kind, r.status) for r in rows] == [
        ("playlist_precheck", "applied"),
        ("playlist_delete", "failed"),
    ]
    assert rows[-1].reason.startswith(QUOTA_REJECTED)
    assert acct.write_calls == []
    assert _status(session, action) == "open"


@pytest.mark.parametrize(
    "knob",
    [
        lambda acct: acct.delete_does_not_land.add("i1"),
        lambda acct: acct.delete_errors.add("i1"),
        lambda acct: acct.youtube_read_errors.update(
            {2: YouTubeWriteError("PL1", "playlist_read", "HTTP 500: boom")}
        ),
    ],
    ids=["accepted", "errored", "unreadable"],
)
def test_a_merge_delete_youtube_cannot_confirm_undoes_nothing(session: Session, knob) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    knob(acct)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.delete_unconfirmed == (("T PL1", 2, "A", "B"),)
    assert res.aborted is not None
    assert (res.applied, res.failed, res.restored) == (0, 1, 0)
    assert acct.write_calls == [("delete", "i1")]
    assert _steps(session, action.item_id)[-1] == ("playlist_delete_verify", "failed")
    assert _status(session, action) == "open"


@pytest.mark.parametrize(
    "knob",
    [
        lambda acct: acct.ytm_reads_returning.update(
            {2: ["v0", "A", "v2", "B"], 3: ["v0", "A", "v2", "B"]}
        ),
        lambda acct: acct.ytm_read_errors.update({2: UnexpectedResponseError("shape changed")}),
    ],
    ids=["differs", "unreadable"],
)
def test_ytmusic_off_after_a_merge_puts_a_back_and_keeps_b(session: Session, knob) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    knob(acct)

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert (res.applied, res.failed, res.skipped, res.restored) == (0, 0, 1, 1)
    assert res.aborted is not None
    assert acct.write_calls == [("delete", "i1"), ("insert", "PL1", "A", "1")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("new1", "A"), ("i2", "v2"), ("i3", "B")]
    assert _steps(session, action.item_id)[-4:] == [
        ("playlist_delete_verify", "applied"),
        ("playlist_check", "failed"),
        *_RESTORE,
    ]
    assert _status(session, action) == "skipped"


def test_ytmusic_differing_after_a_merge_stops_with_playlist_stop(session: Session) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    acct.ytm_reads_returning = {2: ["v0", "A", "v2", "B"], 3: ["v0", "A", "v2", "B"]}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.aborted == PLAYLIST_STOP


def test_a_youtube_side_otherwise_off_after_a_merge_puts_a_back(session: Session) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    delete = acct.delete_playlist_item

    def delete_and_someone_adds(item_id: str) -> None:
        delete(item_id)
        acct.playlists["PL1"].append(("iy", "Y"))

    acct.delete_playlist_item = delete_and_someone_adds  # type: ignore[method-assign]

    res = execute(session, [action], states, ytm=acct, yt=acct)

    # A goes back, but the restore check sees Y too: A isn't confirmed back.
    assert acct.write_calls == [("delete", "i1"), ("insert", "PL1", "A", "1")]
    assert res.left_removed == (("T PL1", 2, "A"),)
    assert _steps(session, action.item_id)[-3:] == [
        ("playlist_delete_verify", "failed"),
        ("playlist_restore", "applied"),
        ("playlist_restore_verify", "failed"),
    ]


def test_ytmusic_off_after_a_merge_in_a_sorted_playlist_puts_a_back_where_it_sorts(
    session: Session,
) -> None:
    acct = _with_b()
    acct.auto_sorted = {"PL1": "end"}
    action, states = _canonical(session, acct, "canonical_merge")
    acct.ytm_reads_returning = {2: ["v0", "A", "v2", "B"], 3: ["v0", "A", "v2", "B"]}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert (res.skipped, res.restored, res.reordered, res.aborted) == (1, 1, 0, PLAYLIST_STOP)
    assert res.sorted_playlists == _SORTED
    assert acct.write_calls == [
        ("delete", "i1"),
        ("insert", "PL1", "A", "1"),  # refused: the playlist is sorted automatically
        ("insert", "PL1", "A"),
    ]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("i3", "B"), ("new1", "A")]
    assert _steps(session, action.item_id)[-4:] == [
        ("playlist_check", "failed"),
        ("playlist_restore", "failed"),
        *_RESTORE,
    ]
    assert _status(session, action) == "skipped"


def test_an_a_that_cannot_be_put_back_after_a_merge_is_reported(session: Session) -> None:
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    acct.ytm_reads_returning = {2: ["v0", "A", "v2", "B"], 3: ["v0", "A", "v2", "B"]}
    acct.insert_errors = {"A"}

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert res.left_removed == (("T PL1", 2, "A"),)
    assert (res.restored, res.aborted) == (0, PLAYLIST_STOP)
    assert acct.write_calls == [("delete", "i1"), ("insert", "PL1", "A", "1")]
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "v2"), ("i3", "B")]
    assert _status(session, action) == "open"


def test_a_merge_delete_that_errored_but_landed_stays_open_and_the_run_goes_on(
    session: Session,
) -> None:
    acct = FakePlaylistAccount(
        {"PL1": [("i0", "v0"), ("i1", "A1"), ("i2", "B1")], "PL2": [("j0", "w0"), ("j1", "A3")]}
    )
    diag = _diagnosis(session)
    merge = _canonical_action(session, diag, acct, "PL1", 1, "B1", "canonical_merge")
    repoint = _canonical_action(session, diag, acct, "PL2", 1, "B3")
    states = _states(acct)
    session.commit()
    acct.delete_errors_after_landing = {"i1"}

    res = execute(session, [merge, repoint], states, ytm=acct, yt=acct)

    assert res == PlaylistExecResult(applied=1, failed=1, skipped=0)
    assert _steps(session, merge.item_id) == [
        ("playlist_precheck", "applied"),
        ("playlist_delete", "failed"),
        ("playlist_delete_verify", "applied"),
        ("playlist_check", "applied"),
    ]
    assert _status(session, merge) == "open"
    assert acct.playlists["PL1"] == [("i0", "v0"), ("i2", "B1")]
    assert acct.playlists["PL2"] == [("j0", "w0"), ("new1", "B3")]


def test_an_interrupt_after_a_merge_delete_call_is_recorded(session: Session) -> None:
    factory = make_session_factory(session.bind)
    acct = _with_b()
    action, states = _canonical(session, acct, "canonical_merge")
    acct.youtube_read_errors[2] = KeyboardInterrupt()  # the delete's check read

    with pytest.raises(KeyboardInterrupt):
        execute(session, [action], states, ytm=acct, yt=acct)

    fresh = factory()
    try:
        assert _steps(fresh, action.item_id) == [
            ("playlist_precheck", "applied"),
            ("playlist_delete", "applied"),
            ("playlist_interrupted", "failed"),
        ]
    finally:
        fresh.close()


@pytest.mark.parametrize(
    ("playlist", "read"),
    [
        ([("i0", "v0"), ("i1", "vidA"), ("i2", "vidB")], 2),  # merge: the delete's check read
        ([("i0", "v0"), ("i1", "vidA")], 3),  # repoint: the delete's check read
    ],
    ids=["merge", "repoint"],
)
def test_an_unrecorded_interrupt_names_a_and_b_and_never_advises_removing_b(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    playlist: list[tuple[str, str]],
    read: int,
) -> None:
    """In a merge B is the entry being kept: removing it while A's delete may
    still land could lose both."""
    acct = FakePlaylistAccount({"PL1": playlist})
    action = _canonical_action(session, _diagnosis(session), acct, "PL1", 1, "vidB")
    states = _states(acct)
    session.commit()
    acct.youtube_read_errors[read] = KeyboardInterrupt()
    commit = session.commit

    def commit_fails_for_the_marker() -> None:
        if any(
            isinstance(o, SyncAttempt) and o.kind == "playlist_interrupted" for o in session.new
        ):
            raise RuntimeError("database is locked")
        commit()

    monkeypatch.setattr(session, "commit", commit_fails_for_the_marker)

    with pytest.raises(KeyboardInterrupt):
        execute(session, [action], states, ytm=acct, yt=acct)

    assert ("delete", "i1") in acct.write_calls
    err = capsys.readouterr().err
    assert "'T PL1'" in err and "vidA" in err and "vidB" in err
    assert "remove" not in err.lower()
    assert "re-scan" in err
