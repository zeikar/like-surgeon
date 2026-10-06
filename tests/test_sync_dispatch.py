"""Tests for the ``sync`` dispatcher (``likesurgeon.sync_dispatch``).

It runs against ``FakeAccount`` (both clients at once): YouTube likes plus how
each renders in YT Music, so every write moves LM the way the rendering model
says (docs/design/ll-lm-alignment.md §2).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.db import make_session_factory
from likesurgeon.diagnosis import ISSUE_SHADOW_DUPLICATE
from likesurgeon.models import DiagnosisItem, SyncAttempt
from likesurgeon.sync import PlannedAction, SkipRecord
from likesurgeon.sync_dispatch import (
    QUOTA_REJECTED,
    RELIKE_STOP,
    RESTORED_STOP,
    ROLLBACK_STOP,
    ExecResult,
    execute,
)
from likesurgeon.sync_preflight import stranded_unliked_video_ids
from likesurgeon.youtube_client import YouTubeWriteError
from likesurgeon.ytmusic_client import UnexpectedResponseError

from .fake_account import FakeAccount
from .sync_items import make_diagnosis, pair, relike, repoint, unlike_shadow, unrendered


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real waits; the requested ones are recorded."""
    waits: list[float] = []
    monkeypatch.setattr("likesurgeon.sync_dispatch.time.sleep", waits.append)
    return waits


def _attempts(session: Session, item_id: int) -> list[SyncAttempt]:
    return list(
        session.scalars(
            select(SyncAttempt)
            .where(SyncAttempt.diagnosis_item_id == item_id)
            .order_by(SyncAttempt.id)
        ).all()
    )


def _steps(session: Session, item_id: int) -> list[tuple[str, str]]:
    return [(r.kind, r.status) for r in _attempts(session, item_id)]


_PRECHECKS = [("repoint_precheck_a", "applied"), ("repoint_precheck_b", "applied")]
_ROLLBACK = [("repoint_rollback", "applied"), ("repoint_rollback_verify", "applied")]
_REPOINT_UNLIKED = [
    *_PRECHECKS,
    ("repoint_like", "applied"),
    ("repoint_verify", "applied"),
    ("repoint_unlike", "applied"),
    ("repoint_unlike_verify", "applied"),
]
_RESTORED = [("lm_check", "failed"), ("restore_like", "applied"), ("restore_verify", "applied")]


# ---------------------------------------------------------------------------
# execute — repoint
# ---------------------------------------------------------------------------


def test_repoint_moves_the_like_and_leaves_lm_unchanged(
    session: Session, sleeps: list[float]
) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    lm_before = sorted(acct.lm)

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert acct.rate_calls == [("B", "like"), ("A", "none")]
    assert "A" not in acct.liked and "B" in acct.liked
    assert sorted(acct.lm) == lm_before
    session.refresh(item)
    assert item.status == "applied"
    assert item.reason == "diagnosis-time evidence"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_REPOINT_UNLIKED, ("lm_check", "applied")]
    assert rows[-1].reason == "as expected: no change"
    assert sleeps == [5, 5]  # each write confirmed after a wait; no LM re-read


def test_repoint_stops_before_unliking_a_when_bs_like_does_not_land(session: Session) -> None:
    """videos.rate("like") on B returned OK but the rating didn't change (seen
    live). Unliking A then would drop the song from both platforms."""
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.like_does_not_land = {"B"}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.rate_calls == [("B", "like")]
    assert "A" in acct.liked
    session.refresh(item)
    assert item.status == "skipped"
    assert _steps(session, item.id) == [
        *_PRECHECKS,
        ("repoint_like", "applied"),
        ("repoint_verify", "skipped"),
    ]


def test_repoint_restores_a_when_its_unlike_drops_another_song(
    session: Session, sleeps: list[float]
) -> None:
    """The diagnosis pairs A with B, but A actually shows as C (B is backed
    by D): unliking A drops C from YT Music. The LM check catches it, re-reads
    once after 15 s, then re-likes A, which brings C back."""
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A", "D"], renders={"A": "C", "D": "B"})

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1, aborted=RESTORED_STOP)
    # B's like is undone too: it was this run's, and the pair was wrong.
    assert acct.rate_calls == [("B", "like"), ("A", "none"), ("A", "like"), ("B", "none")]
    assert "A" in acct.liked and "B" not in acct.liked and "C" in acct.lm
    session.refresh(item)
    assert item.status == "skipped"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_REPOINT_UNLIKED, *_RESTORED, *_ROLLBACK]
    assert rows[6].reason.startswith("expected no change, got -C +B")
    assert sleeps == [5, 5, 15, 5, 5]
    assert acct.lm_reads == 3  # baseline, check, re-read


@pytest.mark.parametrize("restore_breaks", ["rate_error", "like_does_not_land"])
def test_repoint_restore_failure_leaves_a_in_left_unliked(
    session: Session, restore_breaks: str
) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A", "D"], renders={"A": "C", "D": "B"})
    if restore_breaks == "rate_error":
        acct.rate_errors = {("A", "like")}
        tail = [("restore_like", "failed")]
    else:
        acct.like_does_not_land = {"A"}
        tail = [("restore_like", "applied"), ("restore_verify", "failed")]

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0, failed=1, skipped=0, left_unliked=("A",), aborted=RESTORED_STOP
    )
    assert "A" not in acct.liked
    session.refresh(item)
    assert item.status == "open"
    assert _steps(session, item.id) == [*_REPOINT_UNLIKED, ("lm_check", "failed"), *tail]


def test_a_repoint_whose_b_is_already_liked_is_stale_and_skipped(session: Session) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A", "B"], renders={"A": "B", "B": "E"})

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.rate_calls == []
    session.refresh(item)
    assert item.status == "skipped"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [
        ("repoint_precheck_a", "applied"),
        ("repoint_precheck_b", "skipped"),
    ]
    assert "stale" in rows[-1].reason and "compare-likes" in rows[-1].reason


def test_a_failed_restore_also_stops_the_run(session: Session) -> None:
    first, second, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "D", "A2"], renders={"A1": "C", "D": "B1", "A2": "B2"})
    acct.rate_errors = {("A1", "like")}

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res.left_unliked == ("A1",) and res.unattempted == 1
    assert res.aborted == RESTORED_STOP
    assert _attempts(session, second.id) == []


def test_a_like_that_errors_without_landing_leaves_nothing_to_undo(session: Session) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A"], renders={"A": "B"})
    acct.rate_errors = {("B", "like")}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    assert acct.rate_calls == [("B", "like")]
    assert _steps(session, item.id) == [
        *_PRECHECKS,
        ("repoint_like", "failed"),
        ("repoint_verify", "skipped"),
    ]


@pytest.mark.parametrize("breaks", ["rate_error", "unlike_does_not_land"])
def test_a_failed_rollback_of_b_is_reported_without_blocking_a_restore(
    session: Session, breaks: str
) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A", "D"], renders={"A": "C", "D": "B"})
    if breaks == "rate_error":
        acct.rate_errors = {("B", "none")}
        tail = [("repoint_rollback", "failed")]
    else:
        acct.unlike_lands_late = {"B": 99}  # never lands within the run
        tail = [("repoint_rollback", "applied"), ("repoint_rollback_verify", "failed")]

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res.restored == 1 and res.left_liked == ("B",) and res.left_unliked == ()
    assert "A" in acct.liked and "B" in acct.liked
    session.refresh(item)
    assert item.status == "skipped"
    assert _steps(session, item.id)[-len(tail) :] == tail


def test_an_unconfirmed_b_rollback_stops_the_run(session: Session) -> None:
    first, second, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "D", "A2"], renders={"A1": "C", "D": "B1", "A2": "B2"})
    acct.rate_errors = {("B1", "none")}  # not a quota failure

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res.left_liked == ("B1",) and res.restored == 1
    assert res.aborted == ROLLBACK_STOP and res.unattempted == 1
    assert not res.quota_exhausted
    assert _attempts(session, second.id) == []


def test_a_failed_restore_keeps_b_liked(session: Session) -> None:
    """With A unliked and not re-liked, B's like is the song's only one."""
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["A", "D"], renders={"A": "C", "D": "B"})
    acct.rate_errors = {("A", "like")}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res.left_unliked == ("A",) and res.left_liked == ()
    assert ("B", "none") not in acct.rate_calls


@pytest.mark.parametrize("kind", ["repoint", "unlike_shadow"])
def test_a_stale_finding_whose_a_is_no_longer_liked_is_skipped_without_writes(
    session: Session, kind: str
) -> None:
    """The user unliked A after the scan: re-liking it in a restore would undo that."""
    if kind == "repoint":
        item = pair(session, make_diagnosis(session))
        acct = FakeAccount(["x"], renders={"A": "B"})
        action, check = repoint(item), "repoint_precheck_a"
    else:
        item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
        acct = FakeAccount(["x", "B", "B"], renders={})  # B shown twice, A not liked
        action, check = unlike_shadow(item), "unlike_shadow_precheck_a"
    session.commit()

    res = execute(session, [action], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.rate_calls == []
    session.refresh(item)
    assert item.status == "skipped"
    rows = _attempts(session, item.id)
    assert (rows[-1].kind, rows[-1].status) == (check, "skipped")
    assert "rating is not like" in rows[-1].reason


_STILL_LIKED = [
    ("restore_like", "applied"),
    ("restore_verify", "applied"),
    ("lm_check", "applied"),
]


def test_repoint_unlike_that_does_not_take_relikes_a_and_checks_lm(session: Session) -> None:
    """The unlike of A1 errors and A1 still reads liked. It could still land
    late, so A1 is re-liked and LM is checked against A1 never unliked (B1's
    like adds one entry). That check's read is the next action's baseline."""
    diag = make_diagnosis(session)
    items = [pair(session, diag, a=f"A{i}", b=f"B{i}") for i in range(3)]
    session.commit()
    acct = FakeAccount(["A0", "A1", "A2"], renders={f"A{i}": f"B{i}" for i in range(3)})
    acct.rate_errors = {("A1", "none")}
    actions = [repoint(it, f"A{i}", f"B{i}") for i, it in enumerate(items)]

    res = execute(session, actions, [], ytm=acct, yt=acct)

    # The restore stops the run: action 2 is never attempted.
    assert res == ExecResult(
        applied=1, failed=0, skipped=1, restored=1, unattempted=1, aborted=RESTORED_STOP
    )
    assert "A1" in acct.liked and "B1" not in acct.liked  # B1's like was undone
    rows = _attempts(session, items[1].id)
    assert [(r.kind, r.status) for r in rows] == [
        *_PRECHECKS,
        ("repoint_like", "applied"),
        ("repoint_verify", "applied"),
        ("repoint_unlike", "failed"),
        ("repoint_unlike_verify", "failed"),
        *_STILL_LIKED,
        *_ROLLBACK,
    ]
    assert rows[-3].reason == "as expected: +B1"
    for it in items:
        session.refresh(it)
    assert [it.status for it in items] == ["applied", "skipped", "open"]
    assert _attempts(session, items[2].id) == []
    assert acct.lm_reads == 3  # baseline + one check per attempted action


@pytest.mark.parametrize("kind", ["repoint", "unlike_shadow"])
def test_an_unlike_landing_after_its_verify_read_cannot_win(session: Session, kind: str) -> None:
    """getRating(A) still reads 'like', then the unlike lands. Re-liking A
    (a later like wins) keeps it liked, and the LM check confirms nothing left."""
    if kind == "repoint":
        item = pair(session, make_diagnosis(session))
        acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
        action, head = repoint(item), [*_REPOINT_UNLIKED[:5]]
        rate_calls = [("B", "like"), ("A", "none"), ("A", "like"), ("B", "none")]
        tail = _ROLLBACK
    else:
        item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
        acct = FakeAccount(["x", "A", "B", "y"], renders={"A": "B"})
        action = unlike_shadow(item)
        head = [
            ("unlike_shadow_precheck", "applied"),
            ("unlike_shadow_precheck_a", "applied"),
            ("unlike_shadow_unlike", "applied"),
        ]
        rate_calls = [("A", "none"), ("A", "like")]
        tail = []
    session.commit()
    acct.unlike_lands_late = {"A": 1}

    res = execute(session, [action], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1, aborted=RESTORED_STOP)
    assert acct.rate_calls == rate_calls
    assert "A" in acct.liked
    session.refresh(item)
    assert item.status == "skipped"
    verify = "repoint_unlike_verify" if kind == "repoint" else "unlike_shadow_verify"
    assert _steps(session, item.id) == [*head, (verify, "failed"), *_STILL_LIKED, *tail]


def test_an_unlike_landing_after_the_first_relike_is_relike_again(session: Session) -> None:
    """Out of order: the unlike lands only after the first re-like was
    confirmed. LM then shows A's entry gone, so A is re-liked once more."""
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.unlike_lands_late = {"A": 2}  # after the restore_verify read

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1, aborted=RESTORED_STOP)
    assert "A" in acct.liked
    assert _steps(session, item.id) == [
        *_REPOINT_UNLIKED[:5],
        ("repoint_unlike_verify", "failed"),
        *_STILL_LIKED[:2],
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "applied"),
        *_ROLLBACK,
    ]


def test_a_late_unlike_whose_relike_fails_still_gets_the_lm_check(session: Session) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.unlike_lands_late = {"A": 1}
    acct.rate_errors = {("A", "like")}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0, failed=1, skipped=0, left_unliked=("A",), aborted=RESTORED_STOP
    )
    assert "A" not in acct.liked
    assert _steps(session, item.id) == [
        *_REPOINT_UNLIKED[:5],
        ("repoint_unlike_verify", "failed"),
        ("restore_like", "failed"),
        ("lm_check", "failed"),  # A's late unlike took its entry
        ("restore_like", "failed"),
    ]


def test_a_restore_stops_the_run_and_later_actions_stay_untouched(session: Session) -> None:
    """Action 1's LM mismatch proves a pair of this diagnosis was wrong; the
    pairs after it may be too, so nothing more is written."""
    diag = make_diagnosis(session)
    items = [pair(session, diag, a=f"A{i}", b=f"B{i}") for i in range(3)]
    session.commit()
    # A1 really shows as C; B1 is D's.
    acct = FakeAccount(
        ["A0", "A1", "D", "A2"], renders={"A0": "B0", "A1": "C", "D": "B1", "A2": "B2"}
    )
    actions = [repoint(it, f"A{i}", f"B{i}") for i, it in enumerate(items)]

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=1, failed=0, skipped=1, restored=1, unattempted=1, aborted=RESTORED_STOP
    )
    for it in items:
        session.refresh(it)
    assert [it.status for it in items] == ["applied", "skipped", "open"]
    assert _attempts(session, items[2].id) == []
    assert ("B2", "like") not in acct.rate_calls and "A2" in acct.liked


def test_a_matching_check_is_the_next_actions_baseline(session: Session) -> None:
    diag = make_diagnosis(session)
    first = pair(session, diag, a="A1", b="B1")
    second = pair(session, diag, a="A2", b="B2")
    session.commit()
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})

    res = execute(
        session, [repoint(first, "A1", "B1"), repoint(second, "A2", "B2")], [], ytm=acct, yt=acct
    )

    assert res == ExecResult(applied=2, failed=0, skipped=0)
    assert acct.lm_reads == 3  # baseline + one check per action


# ---------------------------------------------------------------------------
# execute — unlike_shadow
# ---------------------------------------------------------------------------


def test_unlike_shadow_removes_one_b_from_lm(session: Session) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
    session.commit()
    acct = FakeAccount(["x", "A", "B", "y"], renders={"A": "B"})  # LM: x B B y

    res = execute(session, [unlike_shadow(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert acct.rate_calls == [("A", "none")]
    assert acct.lm == ["x", "B", "y"]
    session.refresh(item)
    assert item.status == "applied"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [
        ("unlike_shadow_precheck", "applied"),
        ("unlike_shadow_precheck_a", "applied"),
        ("unlike_shadow_unlike", "applied"),
        ("unlike_shadow_verify", "applied"),
        ("lm_check", "applied"),
    ]
    assert rows[-1].reason == "as expected: -B"


def test_unlike_shadow_restores_a_when_the_pair_was_wrong(session: Session) -> None:
    """A actually shows as C; B's second copy is D's. Unliking A drops C."""
    item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
    session.commit()
    acct = FakeAccount(["x", "A", "B", "D"], renders={"A": "C", "D": "B"})  # LM: x C B B

    res = execute(session, [unlike_shadow(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1, aborted=RESTORED_STOP)
    assert acct.rate_calls == [("A", "none"), ("A", "like")]
    assert sorted(acct.lm) == ["B", "B", "C", "x"]
    session.refresh(item)
    assert item.status == "skipped"
    assert _steps(session, item.id)[-3:] == _RESTORED


@pytest.mark.parametrize(
    ("liked", "renders", "why"),
    [
        (["x", "A", "D"], {"A": "B", "D": "B"}, "rating is not like"),  # B itself isn't liked
        (["x", "A", "B", "y"], {"A": None}, "shows B 1 time(s)"),  # no shadow behind B now
    ],
)
def test_unlike_shadow_prechecks_skip_without_writing(
    session: Session, liked: list[str], renders: dict[str, str | None], why: str
) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
    session.commit()
    acct = FakeAccount(liked, renders=renders)

    res = execute(session, [unlike_shadow(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.rate_calls == []
    session.refresh(item)
    assert item.status == "skipped"
    [row] = _attempts(session, item.id)
    assert (row.kind, row.status) == ("unlike_shadow_precheck", "skipped")
    assert why in row.reason


@pytest.mark.parametrize("kind", ["repoint", "unlike_shadow"])
def test_a_failed_rating_read_of_b_is_a_failure_and_a_stays_liked(
    session: Session, kind: str
) -> None:
    """Not a verify miss: the item stays 'open' for a later run."""
    if kind == "repoint":
        item = pair(session, make_diagnosis(session))
        acct = FakeAccount(["A"], renders={"A": "B"})
        action, head, verify = (
            repoint(item),
            [("repoint_precheck_a", "applied")],
            "repoint_precheck_b",
        )
    else:
        item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
        acct = FakeAccount(["A", "B"], renders={"A": "B"})
        action, head, verify = unlike_shadow(item), [], "unlike_shadow_precheck"
    session.commit()
    acct.rating_errors = {"B"}

    res = execute(session, [action], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0)
    assert ("A", "none") not in acct.rate_calls
    session.refresh(item)
    assert item.status == "open"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*head, (verify, "failed")]
    assert "boom" in rows[-1].reason


# ---------------------------------------------------------------------------
# execute — LM read failures
# ---------------------------------------------------------------------------


def test_lm_read_failure_before_the_first_action_runs_nothing(session: Session) -> None:
    diag = make_diagnosis(session)
    items = [pair(session, diag, a=f"A{i}", b=f"B{i}") for i in range(2)]
    report_only = pair(session, diag, confidence=0.5)
    session.commit()
    acct = FakeAccount(["A0", "A1"], renders={"A0": "B0", "A1": "B1"})
    acct.lm_read_errors = {1: UnexpectedResponseError("cookies expired")}
    actions = [repoint(it, f"A{i}", f"B{i}") for i, it in enumerate(items)]
    skip = SkipRecord(report_only.id, "repoint", "report-only finding")

    res = execute(session, actions, [skip], ytm=acct, yt=acct)

    assert (res.applied, res.failed, res.skipped, res.unattempted) == (0, 0, 1, 2)
    assert res.aborted is not None and "cookies expired" in res.aborted
    assert acct.rate_calls == [] and acct.rating_reads == []
    assert all(_attempts(session, it.id) == [] for it in items)
    assert _steps(session, report_only.id) == [("repoint", "skipped")]


def test_lm_read_failure_after_an_unlike_restores_a_and_stops(session: Session) -> None:
    diag = make_diagnosis(session)
    first = pair(session, diag, a="A1", b="B1")
    second = pair(session, diag, a="A2", b="B2")
    session.commit()
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.lm_read_errors = {2: UnexpectedResponseError("network down")}

    res = execute(
        session, [repoint(first, "A1", "B1"), repoint(second, "A2", "B2")], [], ytm=acct, yt=acct
    )

    assert (res.applied, res.failed, res.skipped, res.restored) == (0, 0, 1, 1)
    assert res.unattempted == 1
    assert res.aborted is not None and "network down" in res.aborted
    assert acct.rate_calls == [("B1", "like"), ("A1", "none"), ("A1", "like"), ("B1", "none")]
    assert "A1" in acct.liked and "B1" not in acct.liked
    assert _steps(session, first.id)[-5:] == [
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "applied"),
        *_ROLLBACK,
    ]
    assert _attempts(session, second.id) == []


# ---------------------------------------------------------------------------
# execute — YouTube quota
# ---------------------------------------------------------------------------


def _two_repoints(session: Session) -> tuple[DiagnosisItem, DiagnosisItem, list[PlannedAction]]:
    diag = make_diagnosis(session)
    first = pair(session, diag, a="A1", b="B1")
    second = pair(session, diag, a="A2", b="B2")
    session.commit()
    return first, second, [repoint(first, "A1", "B1"), repoint(second, "A2", "B2")]


def test_quota_on_the_first_write_stops_the_run(session: Session) -> None:
    first, second, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.quota_after = 2  # the two prechecks pass

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True, unattempted=1)
    assert acct.rate_calls == [("B1", "like")]
    assert _steps(session, first.id) == [*_PRECHECKS, ("repoint_like", "failed")]
    assert _attempts(session, second.id) == []


def test_quota_on_the_unlike_leaves_a_liked(session: Session) -> None:
    """The quota error rejects the unlike outright: A is untouched, so B's like
    is undone — which the exhausted quota rejects too, leaving B reported."""
    first, _, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.quota_after = 4  # 2 prechecks + like B1 + getRating B1

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0,
        failed=1,
        skipped=0,
        quota_exhausted=True,
        unattempted=1,
        left_liked=("B1",),
        aborted=ROLLBACK_STOP,
    )
    assert "A1" in acct.liked
    rows = _attempts(session, first.id)
    assert [(r.kind, r.status) for r in rows] == [
        *_REPOINT_UNLIKED[:4],
        ("repoint_unlike", "failed"),
        ("repoint_rollback", "failed"),
    ]
    # Nothing was sent, so an unlike with nothing after it doesn't strand A here.
    assert rows[4].reason.startswith(QUOTA_REJECTED)
    assert stranded_unliked_video_ids(session) == frozenset()


def test_quota_after_the_unlike_still_runs_the_lm_check(session: Session) -> None:
    """getRating(A) hits the quota after A's unlike landed. The LM check needs
    no YouTube quota and passes, so A's unlike stands: nothing is stranded."""
    first, _, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.quota_after = 5

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True, unattempted=1)
    assert _steps(session, first.id) == [
        *_REPOINT_UNLIKED[:5],
        ("repoint_unlike_verify", "failed"),
        ("lm_check", "applied"),
    ]


@pytest.mark.parametrize(
    ("quota_after", "tail"),
    [
        # getRating(A) hits the quota; so does the restore it then needs.
        (
            5,
            [
                ("repoint_unlike_verify", "failed"),
                ("lm_check", "failed"),
                ("restore_like", "failed"),
            ],
        ),
        # The re-like itself hits the quota.
        (
            6,
            [
                ("repoint_unlike_verify", "applied"),
                ("lm_check", "failed"),
                ("restore_like", "failed"),
            ],
        ),
        # The re-like lands, but can't be confirmed.
        (
            7,
            [
                ("repoint_unlike_verify", "applied"),
                ("lm_check", "failed"),
                ("restore_like", "applied"),
                ("restore_verify", "failed"),
            ],
        ),
    ],
)
def test_quota_mid_action_never_leaves_a_unliked_unreported(
    session: Session, quota_after: int, tail: list[tuple[str, str]]
) -> None:
    first, second, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "D", "A2"], renders={"A1": "C", "D": "B1", "A2": "B2"})
    acct.quota_after = quota_after

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0,
        failed=1,
        skipped=0,
        quota_exhausted=True,
        unattempted=1,
        left_unliked=("A1",),
        aborted=RESTORED_STOP,
    )
    assert _steps(session, first.id) == [*_REPOINT_UNLIKED[:5], *tail]
    assert _attempts(session, second.id) == []
    assert stranded_unliked_video_ids(session) == {"A1"}  # and on later runs too


# ---------------------------------------------------------------------------
# execute — skips and commit cadence
# ---------------------------------------------------------------------------


def test_skips_are_recorded_and_leave_items_open(session: Session) -> None:
    item = pair(session, make_diagnosis(session), confidence=0.5)
    session.commit()
    acct = FakeAccount([])

    res = execute(
        session, [], [SkipRecord(item.id, "repoint", "report-only finding")], ytm=acct, yt=acct
    )

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.lm_reads == 0
    session.refresh(item)
    assert item.status == "open"  # plan-time skip ≠ terminal 'skipped'
    assert item.reason == "diagnosis-time evidence"
    [row] = _attempts(session, item.id)
    assert (row.kind, row.status, row.reason) == ("repoint", "skipped", "report-only finding")


def test_execute_commits_per_action(session: Session) -> None:
    """A crash in action 2 must not roll back action 1's rows or status."""
    factory = make_session_factory(session.bind)
    first, second, actions = _two_repoints(session)

    class CrashingAccount(FakeAccount):
        def rate_video(self, video_id: str, rating: str) -> None:
            if video_id == "B2":
                raise RuntimeError("transport crash")
            super().rate_video(video_id, rating)

    acct = CrashingAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    with pytest.raises(RuntimeError, match="transport crash"):
        execute(session, actions, [], ytm=acct, yt=acct)

    fresh = factory()
    try:
        assert fresh.get(DiagnosisItem, first.id).status == "applied"
        assert fresh.get(DiagnosisItem, second.id).status == "open"
        assert _steps(fresh, first.id) == [*_REPOINT_UNLIKED, ("lm_check", "applied")]
        assert _attempts(fresh, second.id) == []
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# execute — interrupts, writes that error yet land, stale LM reads
# ---------------------------------------------------------------------------


def test_an_interrupt_after_the_unlike_is_recorded_and_strands_a(session: Session) -> None:
    """Ctrl-C while the LM check reads: the rows so far plus ``interrupted``
    are committed before the interrupt propagates, so A is reported stranded."""
    factory = make_session_factory(session.bind)
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.lm_read_errors = {2: KeyboardInterrupt()}  # read 1 is the baseline

    with pytest.raises(KeyboardInterrupt):
        execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    fresh = factory()
    try:
        assert _steps(fresh, item.id) == [*_REPOINT_UNLIKED, ("interrupted", "failed")]
        assert fresh.get(DiagnosisItem, item.id).status == "open"
        assert stranded_unliked_video_ids(fresh) == {"A"}
    finally:
        fresh.close()


def test_an_interrupt_whose_row_cant_be_saved_still_names_a(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.lm_read_errors = {2: KeyboardInterrupt()}
    real_commit, commits = session.commit, []

    def commit() -> None:
        commits.append(1)
        if len(commits) == 3:  # after like B and unlike A: the interrupt handler's
            raise RuntimeError("database is locked")
        real_commit()

    monkeypatch.setattr(session, "commit", commit)

    with pytest.raises(KeyboardInterrupt):  # the original, not the DB error
        execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    [line] = capsys.readouterr().err.splitlines()
    assert "stopped after unliking YouTube video A" in line


def test_each_write_row_is_on_disk_before_the_next_wait(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard kill can't be caught, so each write's row must already be
    committed when the wait after it starts: B's like makes the next sync
    refuse until a re-scan, A's unlike also marks A possibly stranded."""
    factory = make_session_factory(session.bind)
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    committed_at_waits: list[list[tuple[str, str]]] = []

    def sleep(_seconds: float) -> None:
        fresh = factory()
        try:
            committed_at_waits.append(_steps(fresh, item.id))
        finally:
            fresh.close()

    monkeypatch.setattr("likesurgeon.sync_dispatch.time.sleep", sleep)

    execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    # Wait 1 confirms B's like; wait 2 confirms A's unlike.
    assert committed_at_waits[0] == [*_PRECHECKS, ("repoint_like", "applied")]
    assert committed_at_waits[1] == _REPOINT_UNLIKED[:5]


def test_an_unlike_that_errors_but_lands_is_still_checked(session: Session) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.rate_errors_after_landing = {("A", "none")}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    # A is unliked as intended and LM checks out, but a call errored.
    assert res == ExecResult(applied=0, failed=1, skipped=0)
    assert "A" not in acct.liked
    assert _steps(session, item.id) == [
        *_REPOINT_UNLIKED[:4],
        ("repoint_unlike", "failed"),
        ("repoint_unlike_verify", "applied"),
        ("lm_check", "applied"),
    ]
    assert stranded_unliked_video_ids(session) == frozenset()


def test_a_like_that_errors_but_lands_makes_the_next_action_reread_lm(session: Session) -> None:
    first, second, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.rate_errors_after_landing = {("B1", "like")}

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=1, skipped=0)
    # The like landed despite the error: B1 is confirmed, then undone (A1 untouched).
    assert _steps(session, first.id) == [
        *_PRECHECKS,
        ("repoint_like", "failed"),
        ("repoint_verify", "applied"),
        *_ROLLBACK,
    ]
    assert "A1" in acct.liked and "B1" not in acct.liked
    session.refresh(second)
    assert second.status == "applied"
    assert acct.lm_reads == 3  # baseline, fresh baseline after B1's stray like, check


def test_a_stale_lm_read_is_settled_by_the_re_read(session: Session, sleeps: list[float]) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
    session.commit()
    acct = FakeAccount(["x", "A", "B", "y"], renders={"A": "B"})
    acct.lm_stale_reads = {2}  # the first post-unlike read still shows B twice

    res = execute(session, [unlike_shadow(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert acct.rate_calls == [("A", "none")]
    assert 15 in sleeps
    assert acct.lm_reads == 3
    assert _steps(session, item.id)[-1] == ("lm_check", "applied")


# ---------------------------------------------------------------------------
# execute — relike
# ---------------------------------------------------------------------------

_RELIKED = [
    ("relike_precheck", "applied"),
    ("relike_unlike", "applied"),
    ("relike_unlike_verify", "applied"),
    ("relike_like", "applied"),
    ("relike_verify", "applied"),
]
# The unlike still reads 'like', then lands right after the re-like's verify.
_LATE_UNLIKE = [
    *_RELIKED[:2],
    ("relike_unlike_verify", "failed"),
    ("relike_like", "applied"),
    ("relike_verify", "applied"),
    ("relike_unlike_pending", "failed"),
    ("relike_lm_check", "applied"),
]


def _unrendered_a(
    session: Session,
    shown_as: str | None = "A",
    account: type[FakeAccount] = FakeAccount,
) -> tuple[DiagnosisItem, FakeAccount]:
    """LL ``x A y`` with A liked but not shown in YT Music; once re-liked, A
    shows as ``shown_as``."""
    item = unrendered(session, make_diagnosis(session))
    session.commit()
    acct = account(["x", "A", "y"], renders={"A": None})
    acct.relike_renders = {"A": shown_as}
    return item, acct


@pytest.mark.parametrize(("shown_as", "diff"), [("A", "+A"), ("T", "+T")])
def test_relike_makes_yt_music_show_a(
    session: Session, sleeps: list[float], shown_as: str, diff: str
) -> None:
    item, acct = _unrendered_a(session, shown_as)

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert acct.rate_calls == [("A", "none"), ("A", "like")]
    assert acct.liked[0] == "A"  # the re-like moved it to the top
    session.refresh(item)
    assert item.status == "applied"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_RELIKED, ("relike_lm_check", "applied")]
    assert rows[-1].reason == f"as expected: {diff}"
    assert sleeps == [5, 5]  # no LM re-read
    assert acct.lm_reads == 2  # baseline, check


def test_a_relike_yt_music_still_does_not_show_is_skipped(
    session: Session, sleeps: list[float]
) -> None:
    item, acct = _unrendered_a(session, None)

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert "A" in acct.liked
    session.refresh(item)
    assert item.status == "skipped"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_RELIKED, ("relike_lm_check", "skipped")]
    assert "still doesn't show" in rows[-1].reason
    assert sleeps == [5, 5, 15]
    assert acct.lm_reads == 3


def test_a_relike_whose_a_is_no_longer_liked_is_skipped_without_writes(session: Session) -> None:
    item = unrendered(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "y"])

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1)
    assert acct.rate_calls == []
    session.refresh(item)
    assert item.status == "skipped"
    assert _steps(session, item.id) == [("relike_precheck", "skipped")]


def test_a_relike_whose_like_does_not_land_leaves_a_unliked(session: Session) -> None:
    """Both re-like attempts read back unliked. The LM check still runs — an
    unrendered A leaves it unchanged — then A is reported and the run stops."""
    item, acct = _unrendered_a(session)
    acct.like_does_not_land = {"A"}

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0, failed=1, skipped=0, left_unliked=("A",), aborted=RELIKE_STOP
    )
    assert "A" not in acct.liked
    session.refresh(item)
    assert item.status == "open"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [
        *_RELIKED[:3],
        ("relike_like", "applied"),
        ("relike_verify", "failed"),
        ("relike_like", "applied"),
        ("relike_verify", "failed"),
        ("relike_lm_check", "applied"),
    ]
    assert rows[-1].reason.startswith("no change")
    assert stranded_unliked_video_ids(session) == {"A"}


def test_a_relike_whose_first_like_errors_is_retried(session: Session) -> None:
    class FirstLikeErrors(FakeAccount):
        def rate_video(self, video_id: str, rating: str) -> None:
            if (video_id, rating) == ("A", "like") and (video_id, rating) not in self.rate_calls:
                self.rate_calls.append((video_id, rating))
                raise YouTubeWriteError(video_id, rating, "boom")
            super().rate_video(video_id, rating)

    item, acct = _unrendered_a(session, account=FirstLikeErrors)

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=0, skipped=0)
    assert acct.rate_calls == [("A", "none"), ("A", "like"), ("A", "like")]
    assert _steps(session, item.id) == [
        *_RELIKED[:3],
        ("relike_like", "failed"),
        *_RELIKED[3:],
        ("relike_lm_check", "applied"),
    ]


@pytest.mark.parametrize(("lands_after", "a_liked"), [(2, True), (3, False), (99, True)])
def test_an_unconfirmed_relike_unlike_gets_a_recovery_relike(
    session: Session, lands_after: int, a_liked: bool
) -> None:
    """getRating(A) still reads 'like' after the unlike, which may land late —
    with 2, right after the re-like's verify, so A is liked nowhere and an
    unrendered A gives the LM check nothing to catch. A is re-liked once more
    after the check; the run stops either way. With 3 the unlike lands even
    after the recovery's verify: A ends unliked with every read having said
    'liked', so A must stay reported stranded until a scan contains it."""
    item, acct = _unrendered_a(session)
    acct.unlike_lands_late = {"A": lands_after}

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, aborted=RELIKE_STOP)
    assert acct.rate_calls == [("A", "none"), ("A", "like"), ("A", "like")]
    assert ("A" in acct.liked) is a_liked
    session.refresh(item)
    assert item.status == "open"
    assert _steps(session, item.id) == [
        *_LATE_UNLIKE,
        ("relike_recover_like", "applied"),
        ("relike_recover_verify", "applied"),
    ]
    assert stranded_unliked_video_ids(session) == {"A"}


@pytest.mark.parametrize("recovery_breaks", ["like_does_not_land", "quota"])
def test_a_failed_recovery_relike_leaves_a_unliked(session: Session, recovery_breaks: str) -> None:
    item, acct = _unrendered_a(session)
    acct.unlike_lands_late = {"A": 2}
    if recovery_breaks == "like_does_not_land":
        acct.like_does_not_land = {"A"}
        tail = [("relike_recover_like", "applied"), ("relike_recover_verify", "failed")]
    else:
        # Rejected outright, so no verify follows: the pending row, not the
        # earlier confirmed verify, is A's newest settling row.
        acct.quota_after = 5  # precheck, unlike, its verify, re-like, its verify
        tail = [("relike_recover_like", "failed")]

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0,
        failed=1,
        skipped=0,
        quota_exhausted=recovery_breaks == "quota",
        left_unliked=("A",),
        aborted=RELIKE_STOP,
    )
    assert "A" not in acct.liked
    assert _steps(session, item.id) == [*_LATE_UNLIKE, *tail]
    assert stranded_unliked_video_ids(session) == {"A"}


def test_a_pending_relike_unlike_is_on_disk_at_every_later_wait(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard kill can't be caught: until a later YouTube scan contains A,
    each wait must find A stranded on disk — even after the first re-like's
    confirmed verify, since the unlike may land after it."""
    factory = make_session_factory(session.bind)
    item, acct = _unrendered_a(session)
    acct.unlike_lands_late = {"A": 2}
    at_waits: list[tuple[float, tuple[str, str], frozenset[str]]] = []

    def sleep(seconds: float) -> None:
        fresh = factory()
        try:
            at_waits.append(
                (seconds, _steps(fresh, item.id)[-1], stranded_unliked_video_ids(fresh))
            )
        finally:
            fresh.close()

    monkeypatch.setattr("likesurgeon.sync_dispatch.time.sleep", sleep)

    execute(session, [relike(item)], [], ytm=acct, yt=acct)

    # The unlike's verify, the re-like's verify, the LM re-read, the recovery's verify.
    assert at_waits == [
        (5, ("relike_unlike", "applied"), {"A"}),
        (5, ("relike_like", "applied"), {"A"}),
        (15, ("relike_unlike_pending", "failed"), {"A"}),
        (5, ("relike_recover_like", "applied"), {"A"}),
    ]
    assert stranded_unliked_video_ids(session) == {"A"}


@pytest.mark.parametrize(("change", "diff"), [("drop_y", "-y +A"), ("add_z", "+A +z")])
def test_a_relike_that_changes_lm_otherwise_stops_the_run(
    session: Session, change: str, diff: str
) -> None:
    """Re-liking A may add its own entry, nothing else: an entry gone, or a
    second one added, means LM changed in a way the diagnosis doesn't explain.
    A stays liked."""

    class OtherChange(FakeAccount):
        def fetch_liked_songs(self, limit: int | None = None) -> list[dict[str, Any]]:
            songs = super().fetch_liked_songs(limit)
            if self.lm_reads == 1:
                return songs
            if change == "drop_y":
                return [s for s in songs if s["videoId"] != "y"]
            return [*songs, {"videoId": "z"}]

    item, acct = _unrendered_a(session, account=OtherChange)

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, aborted=RELIKE_STOP)
    assert "A" in acct.liked
    session.refresh(item)
    assert item.status == "open"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_RELIKED, ("relike_lm_check", "failed")]
    assert rows[-1].reason.startswith(
        f"expected nothing removed and at most one entry added, got {diff}"
    )


def test_lm_read_failure_after_a_relike_stops_the_run(session: Session) -> None:
    item, acct = _unrendered_a(session)
    acct.lm_read_errors = {2: UnexpectedResponseError("network down")}

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert (res.applied, res.failed, res.skipped) == (0, 1, 0)
    assert res.aborted is not None and "network down" in res.aborted
    assert "A" in acct.liked
    assert _steps(session, item.id) == [*_RELIKED, ("relike_lm_check", "failed")]


def test_quota_on_the_relike_unlike_sends_nothing(session: Session) -> None:
    item, acct = _unrendered_a(session)
    acct.quota_after = 1  # the precheck passes

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True)
    assert acct.rate_calls == [("A", "none")]
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == _RELIKED[:1] + [("relike_unlike", "failed")]
    assert rows[-1].reason.startswith(QUOTA_REJECTED)
    assert stranded_unliked_video_ids(session) == frozenset()


def test_quota_on_the_relike_likes_still_runs_the_lm_check(session: Session) -> None:
    item, acct = _unrendered_a(session)
    acct.quota_after = 3  # precheck, unlike, its verify

    res = execute(session, [relike(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(
        applied=0,
        failed=1,
        skipped=0,
        quota_exhausted=True,
        left_unliked=("A",),
        aborted=RELIKE_STOP,
    )
    assert _steps(session, item.id) == [
        *_RELIKED[:3],
        ("relike_like", "failed"),
        ("relike_like", "failed"),
        ("relike_lm_check", "applied"),
    ]
    assert stranded_unliked_video_ids(session) == {"A"}  # newest settling row: the unlike


def test_an_interrupt_during_a_relikes_lm_check_strands_a(session: Session) -> None:
    factory = make_session_factory(session.bind)
    item, acct = _unrendered_a(session)
    acct.lm_read_errors = {2: KeyboardInterrupt()}

    with pytest.raises(KeyboardInterrupt):
        execute(session, [relike(item)], [], ytm=acct, yt=acct)

    fresh = factory()
    try:
        assert _steps(fresh, item.id) == [*_RELIKED, ("interrupted", "failed")]
        assert stranded_unliked_video_ids(fresh) == {"A"}
    finally:
        fresh.close()


def test_two_relikes_check_lm_once_each(session: Session) -> None:
    diag = make_diagnosis(session)
    older = unrendered(session, diag, a="A2")
    newer = unrendered(session, diag, a="A1")
    session.commit()
    acct = FakeAccount(["A1", "x", "A2"], renders={"A1": None, "A2": None})
    acct.relike_renders = {"A1": "A1", "A2": "A2"}

    res = execute(session, [relike(older, "A2"), relike(newer, "A1")], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=2, failed=0, skipped=0)
    assert acct.rate_calls == [("A2", "none"), ("A2", "like"), ("A1", "none"), ("A1", "like")]
    assert acct.lm == ["A1", "A2", "x"]
    assert acct.lm_reads == 3  # baseline + one check per action


def test_a_relike_unlike_that_errors_but_lands_fails_without_stopping(session: Session) -> None:
    """The unlike errors yet lands, and the rest goes as planned: ``failed``
    for the errored call, but A and LM are as expected, so the next relike runs
    against this check's read."""
    diag = make_diagnosis(session)
    older = unrendered(session, diag, a="A2")
    newer = unrendered(session, diag, a="A1")
    session.commit()
    acct = FakeAccount(["A1", "x", "A2"], renders={"A1": None, "A2": None})
    acct.relike_renders = {"A1": "A1", "A2": "A2"}
    acct.rate_errors_after_landing = {("A2", "none")}

    res = execute(session, [relike(older, "A2"), relike(newer, "A1")], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=1, failed=1, skipped=0)
    assert acct.rate_calls == [("A2", "none"), ("A2", "like"), ("A1", "none"), ("A1", "like")]
    session.refresh(older)
    session.refresh(newer)
    assert (older.status, newer.status) == ("open", "applied")
    assert _steps(session, older.id) == [
        _RELIKED[0],
        ("relike_unlike", "failed"),
        *_RELIKED[2:],
        ("relike_lm_check", "applied"),
    ]
    assert acct.lm_reads == 3  # baseline + one check per action
