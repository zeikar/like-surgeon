"""Tests for pairing dead playlist items with what YT Music shows in their place."""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import CanonicalMetadata
from likesurgeon.diagnosis import DIAGNOSIS_KIND_PLAYLISTS, ISSUE_PLAYLIST_RELINKED
from likesurgeon.models import SnapshotItem
from likesurgeon.playlist_repoint import (
    PlaylistAction,
    PlaylistPair,
    PlaylistState,
    find_pairs,
    persist_report,
    plan,
    report_only_cause,
    summarize,
)
from likesurgeon.playlists import load_scan
from likesurgeon.snapshot import PlaylistRead, create_playlist_scan

ATV = "MUSIC_VIDEO_TYPE_ATV"


def _track(video_id: str) -> dict:
    return {
        "videoId": video_id,
        "title": f"T {video_id}",
        "artists": [{"name": "A"}],
        "videoType": ATV,
    }


def _yt(video_id: str, available: bool | None, reason: str | None = None) -> dict:
    item = {
        "snippet": {"title": f"Y {video_id}", "resourceId": {"videoId": video_id}},
        "contentDetails": {"videoId": video_id},
    }
    if available is not None:
        item["_likesurgeon_video_status"] = {"is_available": available, "reason": reason}
    return item


def _read(playlist_id, title, tracks, youtube_items=None, owned=True) -> PlaylistRead:
    return PlaylistRead(
        playlist_id=playlist_id,
        title=title,
        ytmusic={"id": playlist_id, "title": title, "owned": owned, "tracks": tracks},
        youtube_items=youtube_items,
    )


def _ok(video_id: str) -> dict:
    return _yt(video_id, True)


def _blocked(video_id: str) -> dict:
    return _yt(video_id, False, "region-blocked")


def _scan(session: Session, *reads: PlaylistRead):
    create_playlist_scan(session, reads)
    return find_pairs(load_scan(session))


def test_dead_item_in_equal_gap_pairs_with_what_ytmusic_shows(session: Session):
    found = _scan(
        session,
        _read(
            "PLa",
            "Mix",
            [_track("v1"), _track("b1"), _track("v3")],
            [_ok("v1"), _blocked("a1"), _ok("v3")],
        ),
    )
    (pair,) = found.pairs
    assert (pair.a.video_id, pair.a.position, pair.a.is_available) == ("a1", 1, False)
    assert (pair.b.video_id, pair.b.position) == ("b1", 1)
    assert pair.a.snapshot_id != pair.b.snapshot_id
    assert (pair.playlist.source, pair.playlist.playlist_id) == ("ytmusic_playlist", "PLa")
    assert pair.playlist.id == pair.b.snapshot_id
    assert (found.dead_shown_as_itself, found.dead_unplaced) == (0, 0)


def test_dead_item_shown_as_itself_is_counted_not_paired(session: Session):
    found = _scan(
        session,
        _read("PLa", "Mix", [_track("v1"), _track("a1")], [_ok("v1"), _blocked("a1")]),
    )
    assert found.pairs == ()
    assert (found.dead_shown_as_itself, found.dead_unplaced) == (1, 0)


def test_dead_items_in_unequal_gaps_are_unplaced(session: Session):
    found = _scan(
        session,
        # Two dead items shown as one entry: the gap can't be pinned down.
        _read(
            "PLa",
            "Mix",
            [_track("v1"), _track("b1"), _track("v4")],
            [_ok("v1"), _blocked("a1"), _yt("a2", False, "deleted"), _ok("v4")],
        ),
        # A deleted item YT Music doesn't show at all.
        _read(
            "PLb",
            "Other",
            [_track("w1"), _track("w3")],
            [_ok("w1"), _yt("d1", False, "deleted"), _ok("w3")],
        ),
    )
    assert found.pairs == ()
    assert (found.dead_shown_as_itself, found.dead_unplaced) == (0, 3)


def test_playable_or_unchecked_item_shown_as_another_video_is_not_paired(session: Session):
    found = _scan(
        session,
        _read(
            "PLa",
            "Mix",
            [_track("v1"), _track("b1"), _track("v3"), _track("b2")],
            [_ok("v1"), _ok("a1"), _ok("v3"), _yt("a2", None)],
        ),
    )
    assert found.pairs == ()
    assert (found.dead_shown_as_itself, found.dead_unplaced) == (0, 0)


def test_duplicate_dead_video_paired_with_itself_is_not_a_pair(session: Session):
    found = _scan(
        session,
        _read(
            "PLa",
            "Mix",
            [_track("d1"), _track("v2"), _track("d1")],
            [_blocked("d1"), _ok("v2"), _blocked("d1")],
        ),
    )
    assert found.pairs == ()
    assert (found.dead_shown_as_itself, found.dead_unplaced) == (2, 0)


def test_playlist_without_youtube_side_is_skipped(session: Session):
    found = _scan(
        session,
        _read("PLa", "No YouTube", [_track("v1"), _track("b1")]),
        _read("PLb", "Mix", [_track("w1"), _track("b2")], [_ok("w1"), _blocked("a2")]),
    )
    assert [(p.playlist.playlist_id, p.a.video_id, p.b.video_id) for p in found.pairs] == [
        ("PLb", "a2", "b2")
    ]


def _meta(video_id: str, title: str = "Song", channel: str = "UC1", seconds: int = 200):
    return CanonicalMetadata(
        video_id=video_id, title=title, channel_id=channel, duration_seconds=seconds
    )


def _one_pair(session: Session, tracks, youtube_items, owned=True) -> PlaylistPair:
    (pair,) = _scan(session, _read("PLa", "Mix", tracks, youtube_items, owned=owned)).pairs
    return pair


_TRACKS = [_track("v1"), _track("b1"), _track("v3")]
_YOUTUBE = [_ok("v1"), _blocked("a1"), _ok("v3")]
_GOOD_META = {"a1": _meta("a1"), "b1": _meta("b1", seconds=202)}


def test_report_only_cause_none_for_a_clean_pair(session: Session):
    pair = _one_pair(session, _TRACKS, _YOUTUBE)
    assert report_only_cause(pair, _GOOD_META) is None


def test_report_only_cause_saved_playlist_comes_first(session: Session):
    pair = _one_pair(session, _TRACKS, _YOUTUBE, owned=False)
    assert report_only_cause(pair, {}) == "saved playlist"


def test_report_only_cause_unknown_ownership_is_not_acted_on(session: Session):
    pair = _one_pair(session, _TRACKS, _YOUTUBE, owned=None)
    assert report_only_cause(pair, _GOOD_META) == "playlist ownership unknown"


def test_report_only_cause_b_also_on_youtube_side(session: Session):
    # B is listed on the playlist too, so YT Music shows it twice.
    pair = _one_pair(
        session,
        [_track("b1"), _track("b1"), _track("v3")],
        [_ok("b1"), _blocked("a1"), _ok("v3")],
    )
    assert pair.a.video_id == "a1"
    assert report_only_cause(pair, _GOOD_META) == "B is also on the playlist's YouTube side"


def test_report_only_cause_b_shown_more_than_once(session: Session):
    found = _scan(
        session,
        _read(
            "PLa",
            "Mix",
            [_track("v1"), _track("b1"), _track("v3"), _track("b1")],
            [_ok("v1"), _blocked("a1"), _ok("v3"), _blocked("a2")],
        ),
    )
    assert [p.a.video_id for p in found.pairs] == ["a1", "a2"]
    meta = {**_GOOD_META, "a2": _meta("a2")}
    assert {report_only_cause(p, meta) for p in found.pairs} == {"YT Music shows B 2 times"}


def test_report_only_cause_failed_same_recording_check(session: Session):
    pair = _one_pair(session, _TRACKS, _YOUTUBE)
    meta = {"a1": _meta("a1"), "b1": _meta("b1", seconds=260)}
    assert report_only_cause(pair, meta) == "same-recording check failed"


def test_report_only_cause_missing_metadata(session: Session):
    pair = _one_pair(session, _TRACKS, _YOUTUBE)
    assert report_only_cause(pair, {"b1": _meta("b1")}) == "no videos.list metadata for A"
    assert report_only_cause(pair, {"a1": _meta("a1")}) == "no videos.list metadata for B"


def _reportable(session: Session, *reads: PlaylistRead, meta=None):
    scan = create_playlist_scan(session, reads)
    pairs = find_pairs(load_scan(session, scan.id)).pairs
    return (
        scan,
        pairs,
        persist_report(session, scan.id, pairs, _GOOD_META if meta is None else meta),
    )


def test_persist_report_writes_one_item_per_pair(session: Session):
    youtube = [_ok("v1"), _yt("a1", False, "region-blocked"), _ok("v3")]
    scan, (pair,), (diagnosis, carried) = _reportable(
        session, _read("PLa", "Mix", _TRACKS, youtube)
    )
    assert (diagnosis.kind, diagnosis.playlist_scan_id) == (DIAGNOSIS_KIND_PLAYLISTS, scan.id)
    assert carried == 0
    (item,) = diagnosis.items
    assert item.issue_type == ISSUE_PLAYLIST_RELINKED
    assert (item.confidence, item.status) == (1.0, "open")
    assert (item.source_item_id, item.related_item_id) == (pair.a.id, pair.b.id)
    assert (item.source_track_id, item.related_track_id) == (pair.a.track_id, pair.b.track_id)
    for text in ("Mix", "position 2", "a1", "b1", "Y a1", "T b1", "region-blocked"):
        assert text in item.reason
    assert "passed, \u03942s, same channel" in item.reason
    assert "report-only" not in item.reason


def test_persist_report_report_only_cause_lowers_confidence(session: Session):
    _, _, (diagnosis, _) = _reportable(session, _read("PLa", "Mix", _TRACKS, _YOUTUBE, owned=False))
    (item,) = diagnosis.items
    assert item.confidence == 0.5
    assert "saved playlist" in item.reason


def test_persist_report_without_metadata_keeps_the_reason_readable(session: Session):
    _, _, (diagnosis, _) = _reportable(session, _read("PLa", "Mix", _TRACKS, _YOUTUBE), meta={})
    (item,) = diagnosis.items
    assert item.confidence == 0.5
    assert "no videos.list metadata" in item.reason


def test_persist_report_carries_over_a_skipped_pair(session: Session):
    read = _read("PLa", "Mix", _TRACKS, _YOUTUBE)
    _, _, (first, _) = _reportable(session, read)
    first.items[0].status = "skipped"
    _, _, (second, carried) = _reportable(session, read)
    assert carried == 1
    assert second.items[0].status == "skipped"


def test_a_skip_carries_over_only_to_the_same_playlist(session: Session):
    reads = (
        _read("PLa", "Mix", _TRACKS, _YOUTUBE),
        _read("PLb", "Other", _TRACKS, _YOUTUBE),
    )
    _, _, (first, _) = _reportable(session, *reads)
    by_playlist = {
        session.get(SnapshotItem, it.source_item_id).snapshot.playlist_id: it for it in first.items
    }
    by_playlist["PLb"].status = "skipped"
    _, _, (second, carried) = _reportable(session, *reads)
    assert carried == 1
    status = {
        session.get(SnapshotItem, it.source_item_id).snapshot.playlist_id: it.status
        for it in second.items
    }
    assert status == {"PLa": "open", "PLb": "skipped"}


def _linked(video_id: str, item_id: str, position: int, available: bool = True) -> dict:
    item = _ok(video_id) if available else _blocked(video_id)
    item["id"] = item_id
    item["snippet"]["position"] = position
    return item


def _plan_reads():
    return (
        _read(
            "PLb",
            "Big",
            [_track("w1"), _track("b2"), _track("w3"), _track("w4")],
            [_linked("w1", "i1", 0), _linked("a2", "i2", 1, False), _linked("w3", "i3", 2)]
            + [_linked("w4", "i4", 3)],
        ),
        _read(
            "PLa",
            "Small",
            [_track("b1"), _track("v2")],
            [_linked("a1", "j1", 0, False), _linked("v2", "j2", 1)],
        ),
    )


def _planned(session: Session, *reads: PlaylistRead):
    meta = {
        "a1": _meta("a1"),
        "b1": _meta("b1"),
        "a2": _meta("a2"),
        "b2": _meta("b2"),
    }
    _, _, (diagnosis, _) = _reportable(session, *reads, meta=meta)
    return diagnosis, plan(session, diagnosis)


def test_plan_orders_actions_and_describes_each(session: Session):
    diagnosis, (actions, states) = _planned(session, *_plan_reads())
    assert [a.playlist_id for a in actions] == ["PLa", "PLb"]
    small, big = actions
    assert small == PlaylistAction(
        item_id=small.item_id,
        playlist_id="PLa",
        playlist_title="Small",
        a_index=0,
        a_playlist_item_id="j1",
        a_video_id="a1",
        b_video_id="b1",
        b_index=0,
    )
    assert small.item_id in {it.id for it in diagnosis.items}
    assert (big.a_index, big.a_playlist_item_id, big.a_video_id) == (1, "i2", "a2")
    assert (big.b_video_id, big.b_index) == ("b2", 1)


def test_plan_states_hold_both_sequences_per_planned_playlist(session: Session):
    _, (_, states) = _planned(session, *_plan_reads())
    assert states["PLa"] == PlaylistState(
        youtube=(("j1", "a1"), ("j2", "v2")), ytmusic=("b1", "v2")
    )
    assert states["PLb"].youtube[1] == ("i2", "a2")
    assert states["PLb"].ytmusic == ("w1", "b2", "w3", "w4")


def test_plan_skips_items_that_are_not_open_and_confident(session: Session):
    diagnosis, (actions, _) = _planned(session, *_plan_reads())
    skipped = actions[0].item_id
    for it in diagnosis.items:
        if it.id == skipped:
            it.status = "skipped"
        else:
            it.status = "applied"
    session.flush()
    assert plan(session, diagnosis) == ([], {})
    for it in diagnosis.items:
        it.status = "open"
        it.confidence = 0.5
    assert plan(session, diagnosis) == ([], {})


def test_plan_orders_by_playlist_size_then_playlist_id_then_position(session: Session):
    reads = (
        _read(
            "PLz",
            "Z",
            [_track("b1"), _track("b2")],
            [_linked("a1", "z1", 0, False), _linked("a2", "z2", 1, False)],
        ),
        _read("PLa", "A", [_track("b3")], [_linked("a3", "q1", 0, False)]),
        _read(
            "PLb",
            "B",
            [_track("b4"), _track("b5")],
            [_linked("a4", "r1", 0, False), _linked("a5", "r2", 1, False)],
        ),
    )
    meta = {f"{k}{n}": _meta(f"{k}{n}") for k in "ab" for n in range(1, 6)}
    _, _, (diagnosis, _) = _reportable(session, *reads, meta=meta)
    actions, _ = plan(session, diagnosis)
    assert [(a.playlist_id, a.a_index) for a in actions] == [
        ("PLa", 0),
        ("PLb", 0),
        ("PLb", 1),
        ("PLz", 0),
        ("PLz", 1),
    ]


def _summary_quota(n_items: int, n_actions: int) -> str:
    state = PlaylistState(youtube=tuple((f"i{i}", f"v{i}") for i in range(n_items)), ytmusic=())
    actions = [PlaylistAction(k, "PLa", "Mix", 0, "i0", "a", "b", 0) for k in range(n_actions)]
    text = summarize(actions, {"PLa": state})
    assert f"Actions: {n_actions}" in text
    assert "Playlists: 1" in text
    return text


def test_summarize_estimates_quota_by_playlist_size(session: Session):
    for n_items, n_actions, units in ((49, 1, 103), (50, 1, 104), (51, 1, 106), (50, 2, 207)):
        assert f"Estimated YouTube quota: {units} units" in _summary_quota(n_items, n_actions)
