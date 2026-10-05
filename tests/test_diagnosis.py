"""Tests for diagnosis persistence.

These tests use real ``create_snapshot`` calls (rather than fake Track ids)
so that the ``DiagnosisItem.source_track_id`` / ``related_track_id`` foreign
keys resolve under FK enforcement (Task 1 enabled ``PRAGMA foreign_keys=ON``).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.align import align
from likesurgeon.compare import (
    CanonicalMetadata,
    CompareResult,
    Match,
    MatchKind,
    Stage4Evidence,
    UnmatchedItem,
)
from likesurgeon.diagnosis import (
    ALIGNMENT_ISSUE_TYPES,
    ISSUE_DEAD_UNRENDERED,
    ISSUE_DUPLICATE_IN_SOURCE,
    ISSUE_METADATA_DRIFT,
    ISSUE_POINTER_DRIFT,
    ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC,
    ISSUE_RELINKED,
    ISSUE_RENDERED_AS_OTHER,
    ISSUE_SHADOW_DUPLICATE,
    ISSUE_UNAVAILABLE_VIDEO,
    ISSUE_UNBACKED_LM_ENTRY,
    ISSUE_UNRENDERED_MUSIC,
    ISSUE_YTMUSIC_ONLY,
    PAIR_ISSUE_TYPES,
    DiagnosisInput,
    _match_reason,
    build_alignment_items,
    build_duplicate_in_source_items,
    build_unavailable_video_items,
    count_findings,
    create_alignment_diagnosis,
    create_diagnosis,
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


def _summary(item) -> UnmatchedItem:
    import json as _json

    return UnmatchedItem(
        track_id=item.track_id,
        video_id=item.video_id,
        title=item.title,
        artists=_json.loads(item.artists) if item.artists else [],
        canonical_key=item.canonical_key,
    )


def test_create_diagnosis_persists_each_bucket(session: Session):
    """Real Track rows, real ids in the synthetic CompareResult, FK happy."""
    ytm_snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("v_match", "Imagine", ["Lennon"]),
            _ytm_item("v_ytm_only", "YT Music Only", ["Artist"]),
        ],
    )
    yt_snap = create_snapshot(
        session,
        "youtube_liked_videos",
        [
            _yt_item("v_yt_match", "Imagine - John Lennon (Official MV)", "Lennon"),
            _yt_item("v_yt_only", "Cover Song (Official Audio)", "CoverArtist"),
        ],
    )
    session.commit()

    ytm_items = get_snapshot_items(session, ytm_snap.id)
    yt_items = get_snapshot_items(session, yt_snap.id)

    drift_match = Match(
        ytmusic_track_id=ytm_items[0].track_id,
        youtube_track_id=yt_items[0].track_id,
        kind=MatchKind.FUZZY,
        confidence=0.88,
        ytmusic_title=ytm_items[0].title,
        youtube_title=yt_items[0].title,
    )

    result = CompareResult(
        ytmusic_count=2,
        youtube_total_count=2,
        youtube_music_count=2,
        matched=[drift_match],
        possibly_missing_from_ytmusic=[_summary(yt_items[1])],
        ytmusic_only_likes=[_summary(ytm_items[1])],
        pointer_drift_candidates=[drift_match],
    )

    diag = create_diagnosis(
        session,
        DiagnosisInput(
            ytmusic_snapshot_id=ytm_snap.id,
            youtube_snapshot_id=yt_snap.id,
            result=result,
        ),
    )
    session.commit()

    items = diagnosis_items(session, diag.id)
    by_type: dict[str, list] = {}
    for it in items:
        by_type.setdefault(it.issue_type, []).append(it)

    assert ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC in by_type
    assert ISSUE_POINTER_DRIFT in by_type
    assert ISSUE_YTMUSIC_ONLY in by_type
    assert len(by_type[ISSUE_POSSIBLY_MISSING_FROM_YTMUSIC]) == 1
    assert len(by_type[ISSUE_POINTER_DRIFT]) == 1
    assert len(by_type[ISSUE_YTMUSIC_ONLY]) == 1
    # Pointer-drift item should reference both tracks (real ids).
    drift = by_type[ISSUE_POINTER_DRIFT][0]
    assert drift.source_track_id == yt_items[0].track_id
    assert drift.related_track_id == ytm_items[0].track_id
    # All items default to status='open'.
    assert all(it.status == "open" for it in items)


def _make_empty_diagnosis(session: Session) -> Diagnosis:
    diag = Diagnosis(ytmusic_snapshot_id=None, youtube_snapshot_id=None)
    session.add(diag)
    session.flush()
    return diag


def test_build_duplicate_in_source_items_emits_one_per_duplicated_video_id(
    session: Session,
):
    """Snapshot with two items sharing the same video_id yields exactly ONE
    DiagnosisItem per duplicated video_id — never additive on top of
    existing buckets."""
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("dup_vid", "Day by Day", ["X"]),
            _ytm_item("unique_vid", "Other", ["Y"]),
            _ytm_item("dup_vid", "Day by Day", ["X"]),
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)

    result = build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs")

    assert len(result) == 1
    finding = result[0]
    assert finding.issue_type == ISSUE_DUPLICATE_IN_SOURCE
    assert finding.confidence == 1.0
    # source_track_id resolves to the lowest-position row (position=0).
    dup_rows_sorted = sorted(
        [it for it in items if it.video_id == "dup_vid"], key=lambda it: it.position
    )
    assert finding.source_track_id == dup_rows_sorted[0].track_id
    assert finding.related_track_id is None
    assert finding.status == "open"
    assert "appears 2 times" in finding.reason
    # Positions show in ascending order.
    expected_positions = ", ".join(str(it.position) for it in dup_rows_sorted)
    assert expected_positions in finding.reason


def test_build_duplicate_in_source_items_no_duplicates_returns_empty(
    session: Session,
):
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("a", "A", ["X"]),
            _ytm_item("b", "B", ["Y"]),
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)

    assert build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs") == []


def test_build_duplicate_in_source_items_skips_null_video_ids(session: Session):
    """Rows whose video_id is NULL share no identity — they can't be
    duplicates of each other."""
    # ytmusic items without a videoId still translate to SnapshotItem
    # rows (the translator falls back to ``canon:<key>`` for dedupe_key
    # but leaves SnapshotItem.video_id = None).
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            {"title": "Untitled A", "artists": [{"name": "X"}]},
            {"title": "Untitled B", "artists": [{"name": "Y"}]},
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)
    # Sanity: both rows have NULL video_id.
    assert all(it.video_id is None for it in items)

    assert build_duplicate_in_source_items(diag.id, items, "ytmusic_liked_songs") == []


def test_build_duplicate_in_source_items_picks_lowest_position_first(
    session: Session,
):
    """Three rows with the same video_id at distinct positions: builder
    must internally sort by position so ``source_track_id`` resolves to
    the lowest-position row's track and the reason lists positions in
    ascending order regardless of caller's input order."""
    snap = create_snapshot(
        session,
        "ytmusic_liked_songs",
        [
            _ytm_item("v", "A", ["X"]),  # position 0
            _ytm_item("v", "A", ["X"]),  # position 1
            _ytm_item("v", "A", ["X"]),  # position 2
        ],
    )
    session.commit()
    diag = _make_empty_diagnosis(session)
    items = get_snapshot_items(session, snap.id)
    # Shuffle the caller-provided list so we can prove the builder
    # doesn't trust input order.
    shuffled = [items[2], items[0], items[1]]

    result = build_duplicate_in_source_items(diag.id, shuffled, "ytmusic_liked_songs")

    assert len(result) == 1
    finding = result[0]
    # source_track_id == track_id of the position-0 row.
    by_position = sorted(items, key=lambda it: it.position)
    assert finding.source_track_id == by_position[0].track_id
    # Positions render in ascending order in the reason text.
    assert "positions: 0, 1, 2" in finding.reason
    assert "appears 3 times" in finding.reason


def test_latest_diagnosis_orders_by_recency(session: Session):
    """Two empty diagnoses on empty snapshots; latest returns the newer one."""
    ytm_snap = create_snapshot(session, "ytmusic_liked_songs", [])
    yt_snap = create_snapshot(session, "youtube_liked_videos", [])
    session.commit()

    empty = CompareResult(ytmusic_count=0, youtube_total_count=0, youtube_music_count=0)
    inp = DiagnosisInput(
        ytmusic_snapshot_id=ytm_snap.id,
        youtube_snapshot_id=yt_snap.id,
        result=empty,
    )
    first = create_diagnosis(session, inp)
    session.commit()
    second = create_diagnosis(session, inp)
    session.commit()
    assert latest_diagnosis(session).id == second.id
    assert second.id != first.id


def _stage4_match(evidence: Stage4Evidence | None) -> Match:
    """Minimal Match for STAGE4_ENRICHMENT reason-builder tests (no DB needed)."""
    return Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.STAGE4_ENRICHMENT,
        confidence=1.0,
        ytmusic_title="えがお、み~っけた！",
        youtube_title="えがお、み~っけた！ (Official MV)",
        evidence=evidence,
    )


def test_diagnosis_reason_for_stage4_drift_describes_evidence():
    ev = Stage4Evidence(
        channel_id="UCabcdefghij1234",
        duration_seconds=271,
        normalized_title="えがお、み~っけた！",
    )
    reason = _match_reason(_stage4_match(ev))

    assert "UCabcdef" in reason
    assert "271s" in reason
    assert "normalized title match" in reason
    assert "fuzzy" not in reason


def test_diagnosis_reason_for_stage_2_3_fuzzy_drift_unchanged():
    match = Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.FUZZY,
        confidence=0.92,
        ytmusic_title="Imagine",
        youtube_title="Imagine - John Lennon (Official MV)",
    )
    reason = _match_reason(match)

    assert reason.startswith("fuzzy match (score ")


def test_diagnosis_reason_for_stage4_without_evidence_falls_back():
    reason = _match_reason(_stage4_match(evidence=None))

    assert reason == "enriched drift match"


class _FakeSnapshotItem:
    """Duck-typed stand-in for SnapshotItem; only the fields read by
    build_unavailable_video_items matter."""

    def __init__(self, *, track_id: int, is_available: bool | None, unavailable_reason: str | None):
        self.track_id = track_id
        self.is_available = is_available
        self.unavailable_reason = unavailable_reason


def test_build_unavailable_video_items_skips_region_blocked():
    """Region-blocked vids must NOT produce ghost findings — the video still
    exists and may become available again when the restriction lifts."""
    items = [
        _FakeSnapshotItem(track_id=1, is_available=False, unavailable_reason="deleted"),
        _FakeSnapshotItem(track_id=2, is_available=False, unavailable_reason="region_blocked"),
        _FakeSnapshotItem(track_id=3, is_available=False, unavailable_reason="private"),
        _FakeSnapshotItem(track_id=4, is_available=True, unavailable_reason=None),
    ]

    result = build_unavailable_video_items(diagnosis_id=1, snapshot_items=items)

    assert [it.source_track_id for it in result] == [1, 3]
    assert all(it.issue_type == ISSUE_UNAVAILABLE_VIDEO for it in result)
    assert all(it.status == "open" for it in result)
    assert "deleted" in result[0].reason
    assert "private" in result[1].reason


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
                    issue_type=ISSUE_YTMUSIC_ONLY,
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
                    issue_type=ISSUE_YTMUSIC_ONLY,
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


def test_match_reason_for_verified_fuzzy_keeps_stage4_prefix() -> None:
    """The sync gate keys on the reason prefix — a re-verified fuzzy pair must
    read as Stage 4 while still saying where it came from."""
    from likesurgeon.diagnosis import is_stage4_drift_reason

    m = Match(
        ytmusic_track_id=1,
        youtube_track_id=2,
        kind=MatchKind.STAGE4_ENRICHMENT,
        confidence=0.95,
        ytmusic_title="a",
        youtube_title="b",
        evidence=Stage4Evidence(
            channel_id="UCabcdefgh", duration_seconds=250, normalized_title="a", fuzzy_score=100.0
        ),
    )
    reason = _match_reason(m)
    assert reason.startswith("enriched (verified fuzzy 100/100): channel=UCabcdef…")
    assert is_stage4_drift_reason(reason)


# --- alignment findings -------------------------------------------------------


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
        (ISSUE_YTMUSIC_ONLY, 0.5, "open"),  # pre-alignment type: not counted
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
