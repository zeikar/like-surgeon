"""Tests for the read-only playlist report."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from likesurgeon.models import Snapshot
from likesurgeon.playlists import is_backed_up, load_scan, music_kind, playlist_report
from likesurgeon.snapshot import PlaylistRead, create_playlist_scan


@pytest.mark.parametrize("playlist_id", ["LM", "SE", "LRSRabc", "LRYRabc"])
def test_is_backed_up_false_for_system_playlists(playlist_id: str):
    assert is_backed_up(playlist_id) is False


@pytest.mark.parametrize("playlist_id", ["PLabc123", "RDCLAK5uyabc", "OLAK5uyabc"])
def test_is_backed_up_true_for_user_and_album_playlists(playlist_id: str):
    assert is_backed_up(playlist_id) is True


@pytest.mark.parametrize(
    ("video_type", "kind"),
    [
        ("MUSIC_VIDEO_TYPE_ATV", "music"),
        ("MUSIC_VIDEO_TYPE_OMV", "music"),
        ("MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC", "music"),
        ("MUSIC_VIDEO_TYPE_UGC", "ugc"),
        ("MUSIC_VIDEO_TYPE_PODCAST_EPISODE", "other"),
        (None, "other"),
    ],
)
def test_music_kind(video_type: str | None, kind: str):
    assert music_kind(video_type) == kind


def _track(video_id: str, video_type: str | None) -> dict:
    item = {"videoId": video_id, "title": f"T {video_id}", "artists": [{"name": "A"}]}
    if video_type:
        item["videoType"] = video_type
    return item


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


ATV = "MUSIC_VIDEO_TYPE_ATV"
OMV = "MUSIC_VIDEO_TYPE_OMV"
UGC = "MUSIC_VIDEO_TYPE_UGC"


def test_report_none_without_scan(session: Session):
    assert playlist_report(session) is None


def test_report_for_scan_without_snapshots_has_no_rows(session: Session):
    scan = create_playlist_scan(session, [])
    report = playlist_report(session)
    assert report is not None
    assert report.scan_id == scan.id
    assert report.rows == ()
    assert report.dead_items == ()


def test_report_reads_latest_scan_only(session: Session):
    create_playlist_scan(session, [_read("PLold", "Old", [])])
    latest = create_playlist_scan(session, [_read("PLnew", "New", [])])
    report = playlist_report(session)
    assert report.scan_id == latest.id
    assert [r.playlist_id for r in report.rows] == ["PLnew"]


def test_report_row_counts_in_scan_order(session: Session):
    create_playlist_scan(
        session,
        [
            _read(
                "PLb",
                "B list",
                [_track("v1", ATV), _track("v2", OMV), _track("v3", UGC), _track("v4", None)],
                owned=False,
            ),
            _read("PLa", "A list", []),
        ],
    )
    report = playlist_report(session)
    first, second = report.rows
    assert (first.playlist_id, first.title, first.owned) == ("PLb", "B list", False)
    stored = session.get(Snapshot, first.ytmusic_snapshot_id)
    assert (stored.source, stored.playlist_id) == ("ytmusic_playlist", "PLb")
    assert (first.ytmusic_count, first.music, first.ugc, first.other) == (4, 2, 1, 1)
    assert first.music_share == 0.5
    assert second.playlist_id == "PLa"
    assert second.ytmusic_count == 0
    assert second.music_share is None


def test_report_youtube_side_absent(session: Session):
    create_playlist_scan(session, [_read("PLa", "A", [_track("v1", ATV)])])
    row = playlist_report(session).rows[0]
    assert (row.youtube_count, row.dead, row.unchecked) == (None, None, None)


def test_report_youtube_side_with_no_items_counts_zero(session: Session):
    create_playlist_scan(session, [_read("PLa", "A", [_track("v1", ATV)], youtube_items=[])])
    row = playlist_report(session).rows[0]
    assert (row.youtube_count, row.dead, row.unchecked) == (0, 0, 0)


def test_report_counts_dead_and_unchecked_with_dead_items(session: Session):
    create_playlist_scan(
        session,
        [
            _read(
                "PLa",
                "First",
                [_track("v1", ATV)],
                [_yt("v1", True), _yt("v2", False, "deleted"), _yt("v3", None)],
            ),
            _read(
                "PLb",
                "Second",
                [],
                [_yt("v4", False, "private"), _yt("v5", False, "deleted")],
            ),
        ],
    )
    report = playlist_report(session)
    first, second = report.rows
    assert (first.youtube_count, first.dead, first.unchecked) == (3, 1, 1)
    assert (second.youtube_count, second.dead, second.unchecked) == (2, 2, 0)
    assert [
        (d.playlist_title, d.position, d.video_id, d.title, d.reason) for d in report.dead_items
    ] == [
        ("First", 2, "v2", "Y v2", "deleted"),
        ("Second", 1, "v4", "Y v4", "private"),
        ("Second", 2, "v5", "Y v5", "deleted"),
    ]


def test_load_scan_none_without_scan(session: Session):
    assert load_scan(session) is None


def test_load_scan_pairs_each_ytmusic_side_with_its_youtube_side(session: Session):
    older = create_playlist_scan(
        session,
        [
            _read("PLa", "A", [_track("v2", ATV), _track("v1", ATV)], [_yt("v2", True)]),
            _read("PLb", "B", [_track("v3", ATV)]),
        ],
    )
    create_playlist_scan(session, [_read("PLnew", "New", [])])

    loaded = load_scan(session, older.id)
    assert loaded.scan.id == older.id
    first, second = loaded.playlists
    assert (first.ytmusic.source, first.ytmusic.playlist_id) == ("ytmusic_playlist", "PLa")
    assert [i.video_id for i in first.ytmusic_items] == ["v2", "v1"]
    assert (first.youtube.source, first.youtube.playlist_id) == ("youtube_playlist", "PLa")
    assert [i.video_id for i in first.youtube_items] == ["v2"]
    assert second.ytmusic.playlist_id == "PLb"
    assert (second.youtube, second.youtube_items) == (None, None)

    assert [p.ytmusic.playlist_id for p in load_scan(session).playlists] == ["PLnew"]
    assert load_scan(session, older.id + 100) is None
