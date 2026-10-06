"""Tests for the ``sync`` planner: ``resolve_video_ids``, ``plan``, ``summarize``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from likesurgeon.diagnosis import (
    ISSUE_DEAD_UNRENDERED,
    ISSUE_METADATA_DRIFT,
    ISSUE_RELINKED,
    ISSUE_RENDERED_AS_OTHER,
    ISSUE_SHADOW_DUPLICATE,
    ISSUE_UNBACKED_LM_ENTRY,
)
from likesurgeon.models import DiagnosisItem
from likesurgeon.sync import PlannedAction, SkipRecord, plan, resolve_video_ids, summarize

from .sync_items import make_diagnosis, pair, relike, repoint, unlike_shadow, unrendered

# ---------------------------------------------------------------------------
# resolve_video_ids
# ---------------------------------------------------------------------------


def test_resolve_video_ids_maps_both_tracks_and_drops_missing_ids(session: Session) -> None:
    """A track with video_id NULL/empty must NOT appear — the planner relies
    on that absence to emit a SkipRecord."""
    diag = make_diagnosis(session)
    ok = pair(session, diag)
    missing = pair(session, diag, a=None, b="")
    session.commit()

    out = resolve_video_ids(session, [ok, missing])

    assert out == {ok.source_track_id: "A", ok.related_track_id: "B"}


def test_resolve_video_ids_chunks_above_sqlite_limit(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite caps `IN(...)` at 999 params on older builds. Force a small
    batch size and feed enough tracks that we cross multiple chunk boundaries."""
    from likesurgeon import sync as _sync_mod

    monkeypatch.setattr(_sync_mod, "_TRACK_LOOKUP_BATCH_SIZE", 50)

    diag = make_diagnosis(session)
    n = 65  # 130 tracks > 2 × batch — at least 3 chunks
    items = [pair(session, diag, a=f"a{i}", b=f"b{i}") for i in range(n)]
    session.commit()

    out = resolve_video_ids(session, items)

    assert len(out) == 2 * n
    for i, it in enumerate(items):
        assert (out[it.source_track_id], out[it.related_track_id]) == (f"a{i}", f"b{i}")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _plan(
    session: Session,
    items: list[DiagnosisItem],
    availability: dict[int, bool | None] | None = None,
    **kwargs: Any,
) -> tuple[list[PlannedAction], list[SkipRecord]]:
    return plan(items, resolve_video_ids(session, items), availability or {}, **kwargs)


def test_plan_relinked_repoints_the_like_from_a_to_b(session: Session) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_RELINKED)

    assert _plan(session, [item]) == ([repoint(item)], [])


def test_plan_skips_report_only_pairs(session: Session) -> None:
    diag = make_diagnosis(session)
    items = [
        pair(session, diag, issue, confidence=0.5)
        for issue in (ISSUE_RELINKED, ISSUE_RENDERED_AS_OTHER, ISSUE_SHADOW_DUPLICATE)
    ]
    availability = {it.source_track_id: False for it in items}

    actions, skips = _plan(session, items, availability, include_playable=True)

    assert actions == []
    assert [(s.item_id, s.kind) for s in skips] == [
        (items[0].id, "repoint"),
        (items[1].id, "repoint"),
        (items[2].id, "unlike_shadow"),
    ]
    assert all("report-only" in s.reason for s in skips)


def test_plan_rendered_as_other_repoints_only_with_include_playable(session: Session) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_RENDERED_AS_OTHER)

    actions, [skip] = _plan(session, [item])
    assert actions == []
    assert (skip.kind, "--include-playable" in skip.reason) == ("repoint", True)

    assert _plan(session, [item], include_playable=True) == ([repoint(item)], [])


@pytest.mark.parametrize(
    ("available", "include_playable", "planned"),
    [
        (False, False, True),
        (True, False, False),
        (True, True, True),
        (None, True, False),  # unknown availability is report-only
        ("absent", True, False),  # A missing from the scan: unknown too
    ],
)
def test_plan_shadow_duplicate_gates_on_a_availability(
    session: Session, available: bool | None | str, include_playable: bool, planned: bool
) -> None:
    item = pair(session, make_diagnosis(session), ISSUE_SHADOW_DUPLICATE)
    availability = {} if available == "absent" else {item.source_track_id: available}

    actions, skips = _plan(session, [item], availability, include_playable=include_playable)

    if planned:
        assert (actions, skips) == ([unlike_shadow(item)], [])
    else:
        assert actions == []
        assert [s.kind for s in skips] == ["unlike_shadow"]


def test_plan_drops_finished_items_and_non_pair_types_silently(session: Session) -> None:
    diag = make_diagnosis(session)
    items = [
        pair(session, diag, status="applied"),
        pair(session, diag, status="skipped"),
        *(
            pair(session, diag, issue)
            for issue in (
                ISSUE_DEAD_UNRENDERED,
                ISSUE_UNBACKED_LM_ENTRY,
                ISSUE_METADATA_DRIFT,
                "unavailable_video",  # pre-alignment type
            )
        ),
    ]

    assert _plan(session, items, include_playable=True) == ([], [])


def test_plan_skips_pairs_without_video_ids(session: Session) -> None:
    diag = make_diagnosis(session)
    no_a = pair(session, diag, ISSUE_RELINKED, a=None)
    no_b = pair(session, diag, ISSUE_SHADOW_DUPLICATE, b=None)

    actions, skips = _plan(session, [no_a, no_b], {no_b.source_track_id: False})

    assert actions == []
    assert [(s.item_id, s.kind) for s in skips] == [
        (no_a.id, "repoint"),
        (no_b.id, "unlike_shadow"),
    ]
    assert all("no video_id" in s.reason for s in skips)


def test_plan_drops_unrendered_music_without_the_flag(session: Session) -> None:
    item = unrendered(session, make_diagnosis(session))

    assert _plan(session, [item]) == ([], [])


def test_plan_relikes_unrendered_music_with_the_flag(session: Session) -> None:
    item = unrendered(session, make_diagnosis(session))

    assert _plan(session, [item], relike_unrendered=True) == ([relike(item)], [])


def test_plan_drops_finished_unrendered_items_even_with_the_flag(session: Session) -> None:
    diag = make_diagnosis(session)
    items = [
        unrendered(session, diag, status="applied"),
        unrendered(session, diag, status="skipped"),
    ]

    assert _plan(session, items, relike_unrendered=True) == ([], [])


def test_plan_skips_unrendered_music_without_a_video_id(session: Session) -> None:
    item = unrendered(session, make_diagnosis(session), a=None)

    actions, skips = _plan(session, [item], relike_unrendered=True)

    assert actions == []
    assert [(s.item_id, s.kind) for s in skips] == [(item.id, "relike")]
    assert "no video_id" in skips[0].reason


def test_plan_relikes_oldest_first_after_the_pair_actions(session: Session) -> None:
    diag = make_diagnosis(session)
    p = pair(session, diag, ISSUE_RELINKED)
    t0, t1, t2 = (unrendered(session, diag, a=f"a{i}") for i in range(3))
    ll_index = {t0.source_track_id: 0, t1.source_track_id: 5, t2.source_track_id: 2}

    actions, _ = _plan(session, [t0, t1, p, t2], relike_unrendered=True, ll_index=ll_index)

    assert actions == [repoint(p), relike(t1, "a1"), relike(t2, "a2"), relike(t0, "a0")]


def test_plan_relike_missing_from_ll_index_sorts_last(session: Session) -> None:
    diag = make_diagnosis(session)
    unknown, known = unrendered(session, diag, a="u"), unrendered(session, diag, a="k")

    actions, _ = _plan(
        session,
        [unknown, known],
        relike_unrendered=True,
        ll_index={known.source_track_id: 1},
    )

    assert actions == [relike(known, "k"), relike(unknown, "u")]


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_counts_and_quota() -> None:
    actions = [
        PlannedAction(1, "repoint", "a1", "b1"),
        PlannedAction(2, "repoint", "a2", "b2"),
        PlannedAction(3, "unlike_shadow", "a3", "b3"),
    ]
    skips = [SkipRecord(4, "unlike_shadow", "A is a playable video you liked")]

    s = summarize(actions, skips)

    assert "\n  repoint: 2" in s
    assert "\n  unlike_shadow: 1" in s
    assert "skipped: 1" in s
    assert "\n    unlike_shadow: 1" in s
    # 2 × (2 prechecks + like + getRating + unlike + getRating = 104)
    # + (2 prechecks + unlike + getRating = 53).
    assert "261 units" in s


def test_summarize_warns_when_plan_exceeds_daily_quota() -> None:
    actions = [PlannedAction(i, "repoint", f"a{i}", f"b{i}") for i in range(97)]

    assert "exceeds the default daily quota" in summarize(actions, [])
    assert "exceeds" not in summarize(actions[:96], [])  # 96 × 104 = 9984


def test_summarize_counts_relikes_and_quota() -> None:
    actions = [
        PlannedAction(1, "relike", "a1"),
        PlannedAction(2, "relike", "a2"),
        PlannedAction(3, "repoint", "a3", "b3"),
    ]

    s = summarize(actions, [])

    assert "\n  relike: 2" in s
    assert "310 units" in s  # 2 × 103 + 104


def test_summarize_notes_like_order_only_when_relikes_are_planned() -> None:
    assert "like order" not in summarize([PlannedAction(1, "repoint", "a", "b")], [])
    assert "top of Liked videos" in summarize([PlannedAction(1, "relike", "a")], [])
