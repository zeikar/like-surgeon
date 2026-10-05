"""Tests for the ``sync`` dispatcher (``likesurgeon.sync_dispatch``).

It runs against ``FakeAccount`` (both clients at once): YouTube likes plus how
each renders in YT Music, so every write moves LM the way the rendering model
says (docs/design/ll-lm-alignment.md §2).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.db import make_session_factory
from likesurgeon.diagnosis import ISSUE_SHADOW_DUPLICATE
from likesurgeon.models import DiagnosisItem, SyncAttempt
from likesurgeon.sync import PlannedAction, SkipRecord
from likesurgeon.sync_dispatch import QUOTA_REJECTED, ExecResult, execute
from likesurgeon.sync_preflight import stranded_unliked_video_ids
from likesurgeon.ytmusic_client import UnexpectedResponseError

from .fake_account import FakeAccount
from .sync_items import make_diagnosis, pair, repoint, unlike_shadow


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


_REPOINT_UNLIKED = [
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
    assert _steps(session, item.id) == [("repoint_like", "applied"), ("repoint_verify", "skipped")]


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

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1)
    assert acct.rate_calls == [("B", "like"), ("A", "none"), ("A", "like")]
    assert "A" in acct.liked and "C" in acct.lm
    session.refresh(item)
    assert item.status == "skipped"
    rows = _attempts(session, item.id)
    assert [(r.kind, r.status) for r in rows] == [*_REPOINT_UNLIKED, *_RESTORED]
    assert rows[4].reason.startswith("expected no change, got -C +B")
    assert sleeps == [5, 5, 15, 5]
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

    assert res == ExecResult(applied=0, failed=1, skipped=0, left_unliked=("A",))
    assert "A" not in acct.liked
    session.refresh(item)
    assert item.status == "open"
    assert _steps(session, item.id) == [*_REPOINT_UNLIKED, ("lm_check", "failed"), *tail]


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

    assert res == ExecResult(applied=2, failed=0, skipped=1, restored=1)
    assert {"A1", "B1"} <= set(acct.liked)
    rows = _attempts(session, items[1].id)
    assert [(r.kind, r.status) for r in rows] == [
        ("repoint_like", "applied"),
        ("repoint_verify", "applied"),
        ("repoint_unlike", "failed"),
        ("repoint_unlike_verify", "failed"),
        *_STILL_LIKED,
    ]
    assert rows[-1].reason == "as expected: +B1"
    for it in items:
        session.refresh(it)
    assert [it.status for it in items] == ["applied", "skipped", "applied"]
    assert acct.lm_reads == 4  # baseline + one check per action


@pytest.mark.parametrize("kind", ["repoint", "unlike_shadow"])
def test_an_unlike_landing_after_its_verify_read_cannot_win(session: Session, kind: str) -> None:
    """getRating(A) still reads 'like', then the unlike lands. Re-liking A
    (a later like wins) keeps it liked, and the LM check confirms nothing left."""
    if kind == "repoint":
        item = pair(session, make_diagnosis(session))
        acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
        action, head = repoint(item), [*_REPOINT_UNLIKED[:3]]
        rate_calls = [("B", "like"), ("A", "none"), ("A", "like")]
    else:
        item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
        acct = FakeAccount(["x", "A", "B", "y"], renders={"A": "B"})
        action = unlike_shadow(item)
        head = [("unlike_shadow_precheck", "applied"), ("unlike_shadow_unlike", "applied")]
        rate_calls = [("A", "none"), ("A", "like")]
    session.commit()
    acct.unlike_lands_late = {"A": 1}

    res = execute(session, [action], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1)
    assert acct.rate_calls == rate_calls
    assert "A" in acct.liked
    session.refresh(item)
    assert item.status == "skipped"
    verify = "repoint_unlike_verify" if kind == "repoint" else "unlike_shadow_verify"
    assert _steps(session, item.id) == [*head, (verify, "failed"), *_STILL_LIKED]


def test_an_unlike_landing_after_the_first_relike_is_relike_again(session: Session) -> None:
    """Out of order: the unlike lands only after the first re-like was
    confirmed. LM then shows A's entry gone, so A is re-liked once more."""
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.unlike_lands_late = {"A": 2}  # after the restore_verify read

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1)
    assert "A" in acct.liked
    assert _steps(session, item.id) == [
        *_REPOINT_UNLIKED[:3],
        ("repoint_unlike_verify", "failed"),
        *_STILL_LIKED[:2],
        ("lm_check", "failed"),
        ("restore_like", "applied"),
        ("restore_verify", "applied"),
    ]


def test_a_late_unlike_whose_relike_fails_still_gets_the_lm_check(session: Session) -> None:
    item = pair(session, make_diagnosis(session))
    session.commit()
    acct = FakeAccount(["x", "A", "y"], renders={"A": "B"})
    acct.unlike_lands_late = {"A": 1}
    acct.rate_errors = {("A", "like")}

    res = execute(session, [repoint(item)], [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, left_unliked=("A",))
    assert "A" not in acct.liked
    assert _steps(session, item.id) == [
        *_REPOINT_UNLIKED[:3],
        ("repoint_unlike_verify", "failed"),
        ("restore_like", "failed"),
        ("lm_check", "failed"),  # A's late unlike took its entry
        ("restore_like", "failed"),
    ]


def test_after_a_restore_the_next_action_rereads_lm(session: Session) -> None:
    """Action 1's restore leaves B1's extra like in LM; checking action 2
    against action 0's post-check read would wrongly undo it."""
    diag = make_diagnosis(session)
    items = [pair(session, diag, a=f"A{i}", b=f"B{i}") for i in range(3)]
    session.commit()
    # A1 really shows as C; B1 is D's.
    acct = FakeAccount(
        ["A0", "A1", "D", "A2"], renders={"A0": "B0", "A1": "C", "D": "B1", "A2": "B2"}
    )
    actions = [repoint(it, f"A{i}", f"B{i}") for i, it in enumerate(items)]

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=2, failed=0, skipped=1, restored=1)
    for it in items:
        session.refresh(it)
    assert [it.status for it in items] == ["applied", "skipped", "applied"]
    assert acct.lm_reads == 6  # baseline, check 0, check 1 + re-read, fresh baseline, check 2


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

    assert res == ExecResult(applied=0, failed=0, skipped=1, restored=1)
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
        action, head, verify = repoint(item), [("repoint_like", "applied")], "repoint_verify"
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
    assert acct.rate_calls == [("B1", "like"), ("A1", "none"), ("A1", "like")]
    assert "A1" in acct.liked
    assert _steps(session, first.id)[-3:] == _RESTORED
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
    acct.quota_after = 0

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True, unattempted=1)
    assert acct.rate_calls == [("B1", "like")]
    assert _steps(session, first.id) == [("repoint_like", "failed")]
    assert _attempts(session, second.id) == []


def test_quota_on_the_unlike_leaves_a_liked(session: Session) -> None:
    """The quota error rejects the unlike outright: A is untouched."""
    first, _, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.quota_after = 2  # like B1 + getRating B1

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True, unattempted=1)
    assert "A1" in acct.liked
    rows = _attempts(session, first.id)
    assert [(r.kind, r.status) for r in rows] == [
        *_REPOINT_UNLIKED[:2],
        ("repoint_unlike", "failed"),
    ]
    # Nothing was sent, so an unlike with nothing after it doesn't strand A here.
    assert rows[-1].reason.startswith(QUOTA_REJECTED)
    assert stranded_unliked_video_ids(session) == frozenset()


def test_quota_after_the_unlike_still_runs_the_lm_check(session: Session) -> None:
    """getRating(A) hits the quota after A's unlike landed. The LM check needs
    no YouTube quota and passes, so A's unlike stands: nothing is stranded."""
    first, _, actions = _two_repoints(session)
    acct = FakeAccount(["A1", "A2"], renders={"A1": "B1", "A2": "B2"})
    acct.quota_after = 3

    res = execute(session, actions, [], ytm=acct, yt=acct)

    assert res == ExecResult(applied=0, failed=1, skipped=0, quota_exhausted=True, unattempted=1)
    assert _steps(session, first.id) == [
        *_REPOINT_UNLIKED[:3],
        ("repoint_unlike_verify", "failed"),
        ("lm_check", "applied"),
    ]


@pytest.mark.parametrize(
    ("quota_after", "tail"),
    [
        # getRating(A) hits the quota; so does the restore it then needs.
        (
            3,
            [
                ("repoint_unlike_verify", "failed"),
                ("lm_check", "failed"),
                ("restore_like", "failed"),
            ],
        ),
        # The re-like itself hits the quota.
        (
            4,
            [
                ("repoint_unlike_verify", "applied"),
                ("lm_check", "failed"),
                ("restore_like", "failed"),
            ],
        ),
        # The re-like lands, but can't be confirmed.
        (
            5,
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
    )
    assert _steps(session, first.id) == [*_REPOINT_UNLIKED[:3], *tail]
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
    assert committed_at_waits[0] == [("repoint_like", "applied")]
    assert committed_at_waits[1] == _REPOINT_UNLIKED[:3]


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
        *_REPOINT_UNLIKED[:2],
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
    assert _steps(session, first.id) == [("repoint_like", "failed")]
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
