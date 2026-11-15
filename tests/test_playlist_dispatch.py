"""Tests for the playlist repoint dispatcher (``likesurgeon.playlist_dispatch``).

It runs against ``FakePlaylistAccount`` (both clients at once): each
playlist's YouTube items plus how each video renders in YT Music, so every
insert and delete moves the YT Music side the way the rendering model says
(docs/design/playlist-repoint.md).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.diagnosis import DIAGNOSIS_KIND_PLAYLISTS, ISSUE_PLAYLIST_RELINKED
from likesurgeon.models import Diagnosis, DiagnosisItem, SyncAttempt
from likesurgeon.playlist_dispatch import PlaylistExecResult, execute
from likesurgeon.playlist_repoint import PlaylistAction, PlaylistState
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

    row = _attempts(session, action.item_id)[-1]
    assert (row.kind, row.status) == ("playlist_insert_verify", "failed")
    assert row.reason == f"read failed: YouTube: {acct.youtube_read_errors[2]}"
    assert res.aborted is not None and "HTTP 500: boom" in res.aborted


def test_quota_on_a_check_read_names_the_quota_not_a_failed_repoint(session: Session) -> None:
    acct = _one_playlist()
    action = _action(session, _diagnosis(session), acct, "PL1", 1)
    states = _states(acct)
    session.commit()
    acct.quota_after = 2  # baseline read and insert; the insert's check read is refused

    res = execute(session, [action], states, ytm=acct, yt=acct)

    assert (res.quota_exhausted, res.aborted) == (True, None)
    row = _attempts(session, action.item_id)[-1]
    assert (row.kind, row.status) == ("playlist_insert_verify", "failed")
    assert "quotaExceeded" in row.reason
