"""Tests for pairing dead playlist items with what YT Music shows in their place."""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.compare import CanonicalMetadata
from likesurgeon.playlist_repoint import PlaylistPair, find_pairs, report_only_cause
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
