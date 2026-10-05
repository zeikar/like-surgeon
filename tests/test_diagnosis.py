"""Tests for diagnosis persistence.

These tests use real ``create_snapshot`` calls (rather than fake Track ids)
so that the ``DiagnosisItem.source_track_id`` / ``related_track_id`` foreign
keys resolve under FK enforcement (Task 1 enabled ``PRAGMA foreign_keys=ON``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.align import align
from likesurgeon.compare import CanonicalMetadata
from likesurgeon.diagnosis import (
    ALIGNMENT_ISSUE_TYPES,
    ISSUE_DEAD_UNRENDERED,
    ISSUE_METADATA_DRIFT,
    ISSUE_RELINKED,
    ISSUE_RENDERED_AS_OTHER,
    ISSUE_SHADOW_DUPLICATE,
    ISSUE_UNBACKED_LM_ENTRY,
    ISSUE_UNRENDERED_MUSIC,
    PAIR_ISSUE_TYPES,
    build_alignment_items,
    count_findings,
    create_alignment_diagnosis,
    diagnosis_items,
    is_write_eligible,
    latest_diagnosis,
)
from likesurgeon.models import Diagnosis, DiagnosisItem
from likesurgeon.snapshot import create_snapshot, get_snapshot_items


def _ytm_item(video_id: str, title: str, artists: list[str]) -> dict:
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artists]}


def _yt_item(video_id: str, title: str, channel: str = "ArtistVEVO") -> dict:
    return {
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "description": "",
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {"videoId": video_id},
    }


def _make_empty_diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    return diag


def test_latest_diagnosis_orders_by_recency(session: Session):
    first = _make_empty_diagnosis(session)
    second = _make_empty_diagnosis(session)
    session.commit()
    assert latest_diagnosis(session).id == second.id
    assert second.id != first.id


def test_carry_over_skipped_keeps_terminal_skip_across_diagnoses(session: Session) -> None:
    """compare-likes writes fresh 'open' items each run; a 'skipped' finding
    (verify-miss or manual suppression) must stay skipped in the next one."""
    from likesurgeon.diagnosis import carry_over_skipped
    from likesurgeon.models import Diagnosis, DiagnosisItem, Track

    tracks = []
    for vid in ("a", "b"):
        t = Track(
            source="ytmusic_liked_songs",
            video_id=vid,
            title=vid,
            artists="[]",
            canonical_key=vid,
            dedupe_key=vid,
        )
        session.add(t)
        tracks.append(t)
    session.flush()

    def _diag(statuses: dict[int, str]) -> Diagnosis:
        d = Diagnosis()
        session.add(d)
        session.flush()
        for t in tracks:
            session.add(
                DiagnosisItem(
                    diagnosis_id=d.id,
                    issue_type="ytmusic_only",
                    confidence=0.5,
                    reason="r",
                    source_track_id=t.id,
                    status=statuses.get(t.id, "open"),
                )
            )
        session.flush()
        return d

    first = _diag({tracks[0].id: "skipped", tracks[1].id: "applied"})
    second = _diag({})

    assert carry_over_skipped(session, second) == 1
    by_track = {it.source_track_id: it.status for it in diagnosis_items(session, second.id)}
    # Only 'skipped' carries; an 'applied' finding that reappears is retried.
    assert by_track == {tracks[0].id: "skipped", tracks[1].id: "open"}
    assert first.id != second.id


def test_carry_over_skipped_noop_without_previous_diagnosis(session: Session) -> None:
    from likesurgeon.diagnosis import carry_over_skipped
    from likesurgeon.models import Diagnosis

    d = Diagnosis()
    session.add(d)
    session.flush()
    assert carry_over_skipped(session, d) == 0


def test_carry_over_skipped_bridges_gaps_and_respects_unskip(session: Session) -> None:
    """Status comes from the most recent earlier diagnosis that HAS the
    finding: a diagnosis missing it doesn't drop the skip, and an operator
    flipping it back to 'open' on the latest one un-skips it."""
    from likesurgeon.diagnosis import carry_over_skipped
    from likesurgeon.models import Diagnosis, DiagnosisItem, Track

    a = Track(
        source="ytmusic_liked_songs",
        video_id="a",
        title="a",
        artists="[]",
        canonical_key="a",
        dedupe_key="a",
    )
    b = Track(
        source="ytmusic_liked_songs",
        video_id="b",
        title="b",
        artists="[]",
        canonical_key="b",
        dedupe_key="b",
    )
    session.add_all([a, b])
    session.flush()

    def _diag(statuses: dict[int, str]) -> Diagnosis:
        d = Diagnosis()
        session.add(d)
        session.flush()
        for track_id, status in statuses.items():
            session.add(
                DiagnosisItem(
                    diagnosis_id=d.id,
                    issue_type="ytmusic_only",
                    confidence=0.5,
                    reason="r",
                    source_track_id=track_id,
                    status=status,
                )
            )
        session.flush()
        return d

    _diag({a.id: "skipped", b.id: "skipped"})
    _diag({b.id: "open"})  # a absent (gap); b un-skipped by the operator
    latest = _diag({a.id: "open", b.id: "open"})

    assert carry_over_skipped(session, latest) == 1
    by_track = {it.source_track_id: it.status for it in diagnosis_items(session, latest.id)}
    assert by_track == {a.id: "skipped", b.id: "open"}


def _meta(video_id: str, title: str = "Song", channel_id: str = "UC1", duration: int = 200):
    return CanonicalMetadata(
        video_id=video_id, title=title, channel_id=channel_id, duration_seconds=duration
    )


def _aligned(session: Session, ll_ids: list[str], lm_ids: list[str], *, ll_state=None):
    """Snapshot both lists (newest first) and align them.

    ``ll_state`` maps an LL video_id to ``(is_available, unavailable_reason,
    is_music_candidate)`` overrides applied to the stored rows.
    """
    ytm = create_snapshot(
        session, "ytmusic_liked_songs", [_ytm_item(v, f"T {v}", ["X"]) for v in lm_ids]
    )
    yt = create_snapshot(session, "youtube_liked_videos", [_yt_item(v, f"T {v}") for v in ll_ids])
    ll_rows = get_snapshot_items(session, yt.id)
    for row in ll_rows:
        avail, reason, music = (ll_state or {}).get(row.video_id, (True, None, True))
        row.is_available, row.unavailable_reason, row.is_music_candidate = avail, reason, music
    session.flush()
    lm_rows = get_snapshot_items(session, ytm.id)
    return ytm, yt, ll_rows, lm_rows, align(ll_rows, lm_rows)


def _by_type(items):
    return {it.issue_type: it for it in items}


def test_alignment_constants_include_the_new_types_and_metadata_drift():
    assert set(ALIGNMENT_ISSUE_TYPES) == {
        ISSUE_RELINKED,
        ISSUE_RENDERED_AS_OTHER,
        ISSUE_SHADOW_DUPLICATE,
        ISSUE_DEAD_UNRENDERED,
        ISSUE_UNRENDERED_MUSIC,
        ISSUE_UNBACKED_LM_ENTRY,
        ISSUE_METADATA_DRIFT,
    }


def test_rendered_pair_with_unavailable_a_is_relinked_and_write_eligible(session: Session):
    _, _, ll, lm, res = _aligned(
        session, ["a1", "A", "a3"], ["a1", "B", "a3"], ll_state={"A": (False, "deleted", True)}
    )
    meta = {"A": _meta("A"), "B": _meta("B")}
    [item] = build_alignment_items(1, res, metadata=meta)
    assert item.issue_type == ISSUE_RELINKED
    assert item.confidence == 1.0
    assert item.source_track_id == ll[1].track_id
    assert item.related_track_id == lm[1].track_id
    assert item.status == "open"
    assert "\n" not in item.reason
    for needle in (
        "YouTube video A 'T A' (LL #1, unavailable: deleted)",
        "YT Music as B 'T B' (LM #1)",
        "same-recording check: passed, \u03940s, same channel",
    ):
        assert needle in item.reason
    assert "report-only" not in item.reason


def test_rendered_pair_with_available_a_is_rendered_as_other(session: Session):
    _, _, _, _, res = _aligned(session, ["a1", "A", "a3"], ["a1", "B", "a3"])
    [item] = build_alignment_items(1, res, metadata={"A": _meta("A"), "B": _meta("B")})
    assert item.issue_type == ISSUE_RENDERED_AS_OTHER
    assert item.confidence == 1.0


def test_rendered_pair_whose_b_is_liked_on_youtube_is_shadow_duplicate(session: Session):
    # B is in LL (at the end); LM shows it once as a shadow for A and once as itself.
    _, _, _, _, res = _aligned(
        session, ["A", "x", "B"], ["B", "x", "B"], ll_state={"A": (False, "deleted", True)}
    )
    items = build_alignment_items(1, res, metadata={"A": _meta("A"), "B": _meta("B")})
    assert [i.issue_type for i in items] == [ISSUE_SHADOW_DUPLICATE]


def test_pair_is_report_only_without_metadata_or_sanity_or_known_availability(session: Session):
    _, _, _, _, res = _aligned(session, ["a1", "A", "a3"], ["a1", "B", "a3"])
    [no_meta] = build_alignment_items(1, res, metadata={"A": _meta("A")})
    assert no_meta.confidence == 0.5
    assert "report-only: no videos.list metadata" in no_meta.reason

    bad = {"A": _meta("A", duration=200), "B": _meta("B", duration=300)}
    [failed] = build_alignment_items(1, res, metadata=bad)
    assert failed.confidence == 0.5
    assert "report-only: same-recording check failed" in failed.reason

    _, _, _, _, res2 = _aligned(
        session, ["a1", "A", "a3"], ["a1", "B", "a3"], ll_state={"A": (None, None, True)}
    )
    [unknown] = build_alignment_items(1, res2, metadata={"A": _meta("A"), "B": _meta("B")})
    assert unknown.confidence == 0.5
    assert "report-only: availability unknown" in unknown.reason


def test_unbacked_lm_entry_is_reported_on_the_lm_track(session: Session):
    # Two LM entries between anchors but one LL video: gap can't be pinned down.
    _, _, ll, lm, res = _aligned(session, ["a1", "A", "a3"], ["a1", "B1", "B2", "a3"])
    items = build_alignment_items(1, res, metadata={})
    assert [i.issue_type for i in items] == [ISSUE_UNBACKED_LM_ENTRY] * 2
    assert [i.source_track_id for i in items] == [lm[1].track_id, lm[2].track_id]
    assert all(i.confidence == 1.0 and i.related_track_id is None for i in items)


def test_unrendered_ll_dead_music_and_other(session: Session):
    # LM omits the three middle LL videos; they sit in a gap with zero LM entries.
    _, _, ll, _, res = _aligned(
        session,
        ["a1", "dead", "song", "clip", "unk", "a6"],
        ["a1", "a6"],
        ll_state={
            "dead": (False, "private", False),
            "song": (True, None, True),
            "clip": (True, None, False),
            "unk": (None, None, True),
        },
    )
    items = _by_type(build_alignment_items(1, res, metadata={}))
    assert len(build_alignment_items(1, res, metadata={})) == 2
    assert set(items) == {ISSUE_DEAD_UNRENDERED, ISSUE_UNRENDERED_MUSIC}
    assert items[ISSUE_DEAD_UNRENDERED].source_track_id == ll[1].track_id
    assert "unavailable: private" in items[ISSUE_DEAD_UNRENDERED].reason
    assert items[ISSUE_UNRENDERED_MUSIC].source_track_id == ll[2].track_id


def test_ambiguous_ll_indices_produce_no_finding(session: Session):
    # Gap has 1 LM entry for 2 LL videos: unbacked, and the LL videos are ambiguous.
    _, _, _, _, res = _aligned(
        session,
        ["a1", "x", "y", "a4"],
        ["a1", "B", "a4"],
        ll_state={"x": (False, "deleted", True), "y": (True, None, True)},
    )
    assert res.ambiguous_ll == frozenset({1, 2})
    assert [i.issue_type for i in build_alignment_items(1, res, metadata={})] == [
        ISSUE_UNBACKED_LM_ENTRY
    ]


def test_pair_with_null_ll_video_id_is_report_only(session: Session):
    ytm = create_snapshot(
        session, "ytmusic_liked_songs", [_ytm_item(v, v, ["X"]) for v in ("a1", "B", "a3")]
    )
    yt = create_snapshot(
        session, "youtube_liked_videos", [_yt_item(v, v) for v in ("a1", "A", "a3")]
    )
    ll = get_snapshot_items(session, yt.id)
    ll[1].video_id = None
    session.flush()
    res = align(ll, get_snapshot_items(session, ytm.id))
    [item] = build_alignment_items(1, res, metadata={"B": _meta("B")})
    assert item.issue_type == ISSUE_RENDERED_AS_OTHER
    assert item.confidence == 0.5


def test_create_alignment_diagnosis_persists_row_and_items(session: Session):
    ytm, yt, _, _, res = _aligned(
        session, ["a1", "A", "a3"], ["a1", "B", "a3"], ll_state={"A": (False, "deleted", True)}
    )
    diag = create_alignment_diagnosis(
        session,
        ytmusic_snapshot_id=ytm.id,
        youtube_snapshot_id=yt.id,
        result=res,
        metadata={"A": _meta("A"), "B": _meta("B")},
    )
    assert diag.ytmusic_snapshot_id == ytm.id and diag.youtube_snapshot_id == yt.id
    [item] = diagnosis_items(session, diag.id)
    assert item.issue_type == ISSUE_RELINKED


def test_write_eligibility_and_finding_counts(session: Session):
    diag = _make_empty_diagnosis(session)
    rows = [
        (ISSUE_RELINKED, 1.0, "open"),
        (ISSUE_RELINKED, 1.0, "skipped"),  # eligible, but carried-over skip
        (ISSUE_RELINKED, 0.5, "open"),  # report-only pair
        (ISSUE_SHADOW_DUPLICATE, 1.0, "open"),
        (ISSUE_UNBACKED_LM_ENTRY, 1.0, "open"),  # a fact, never actionable
        ("ytmusic_only", 0.5, "open"),  # pre-alignment type: not counted
    ]
    items = [
        DiagnosisItem(diagnosis_id=diag.id, issue_type=t, confidence=c, reason="r", status=s)
        for t, c, s in rows
    ]
    session.add_all(items)
    session.flush()

    assert PAIR_ISSUE_TYPES == (ISSUE_RELINKED, ISSUE_RENDERED_AS_OTHER, ISSUE_SHADOW_DUPLICATE)
    assert [is_write_eligible(it) for it in items] == [True, True, False, True, False, False]

    counts = count_findings(session, diag.id)
    assert counts.total == {
        **dict.fromkeys(ALIGNMENT_ISSUE_TYPES, 0),
        ISSUE_RELINKED: 3,
        ISSUE_SHADOW_DUPLICATE: 1,
        ISSUE_UNBACKED_LM_ENTRY: 1,
    }
    # Eligible and still open: the skipped relinked one isn't counted.
    assert counts.eligible == {
        ISSUE_RELINKED: 1,
        ISSUE_RENDERED_AS_OTHER: 0,
        ISSUE_SHADOW_DUPLICATE: 1,
    }
