"""Tests for playlist canonicalize: discovery, report and planner
(``likesurgeon.playlist_canonicalize``)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from likesurgeon.diagnosis import (
    DIAGNOSIS_KIND_PLAYLIST_CANONICALIZE,
    ISSUE_PLAYLIST_CANONICAL_VERSION,
    ISSUE_PLAYLIST_RELINKED,
)
from likesurgeon.models import Diagnosis, DiagnosisItem, SnapshotItem, Track
from likesurgeon.playlist_canonicalize import (
    artist_ids,
    find,
    persist_report,
    plan,
    summarize,
)
from likesurgeon.playlist_repoint import PlaylistState
from likesurgeon.playlists import load_scan
from likesurgeon.snapshot import PlaylistRead, create_playlist_scan
from likesurgeon.ytmusic_client import ArtistSongs

ATV = "MUSIC_VIDEO_TYPE_ATV"
OMV = "MUSIC_VIDEO_TYPE_OMV"


def _atv(video_id: str, title: str = "Song", *, artist: str = "AR1", album: str = "Single") -> dict:
    """An audio track by ``artist`` (an artist id), 200 s long."""
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": f"Name {artist}", "id": artist}],
        "videoType": ATV,
        "duration_seconds": 200,
        "album": {"name": album},
    }


def _filler(video_id: str) -> dict:
    """A track discovery ignores (not an audio track, no artist id)."""
    return {"videoId": video_id, "title": f"F {video_id}", "artists": [], "videoType": OMV}


def _yt(video_id: str, item_id: str, position: int, available: bool | None = True) -> dict:
    item = {
        "id": item_id,
        "snippet": {"title": video_id, "position": position, "resourceId": {"videoId": video_id}},
        "contentDetails": {"videoId": video_id},
    }
    if available is not None:
        reason = None if available else "deleted"
        item["_likesurgeon_video_status"] = {"is_available": available, "reason": reason}
    return item


def _side(playlist_id: str, *videos: str | tuple[str, bool | None]) -> list[dict]:
    """YouTube items in order; a bare id is playable, ``(id, available)`` sets it."""
    out = []
    for i, v in enumerate(videos):
        video_id, available = v if isinstance(v, tuple) else (v, True)
        out.append(_yt(video_id, f"{playlist_id}-{i}", i, available))
    return out


def _read(
    playlist_id: str,
    title: str,
    tracks: list[dict],
    youtube_items: list[dict] | None,
    owned: bool | None = True,
) -> PlaylistRead:
    return PlaylistRead(
        playlist_id=playlist_id,
        title=title,
        ytmusic={"id": playlist_id, "title": title, "owned": owned, "tracks": tracks},
        youtube_items=youtube_items,
    )


def _page(*songs: dict, top: tuple[str, ...] = ()) -> ArtistSongs:
    return ArtistSongs(top_video_ids=top, songs=songs)


# The release AR1's page lists for "Song".
_B = _atv("B", album="Album")
_ARTISTS = {"AR1": _page(_B)}


def _mix(*extra_tracks: dict, extra_youtube: tuple = (), album: str = "Single") -> PlaylistRead:
    """PLa 'Mix': filler f0, then A (shown as itself), then the extras."""
    tracks = [_filler("f0"), _atv("A", album=album), *extra_tracks]
    youtube = ("f0", "A", *extra_youtube)
    return _read("PLa", "Mix", tracks, _side("PLa", *youtube))


def _found(session: Session, *reads: PlaylistRead, artists=None):
    create_playlist_scan(session, reads)
    scan = load_scan(session)
    return scan, find(scan, _ARTISTS if artists is None else artists)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_a_playable_a_shown_as_itself_with_one_listed_b_is_a_finding(session: Session) -> None:
    _, found = _found(session, _mix())

    (pair,) = found.pairs
    assert pair.playlist.ytmusic.playlist_id == "PLa"
    assert (pair.a.snapshot.source, pair.a.video_id, pair.a.position) == (
        "youtube_playlist",
        "A",
        1,
    )
    assert (pair.shown.snapshot.source, pair.shown.video_id, pair.shown.position) == (
        "ytmusic_playlist",
        "A",
        1,
    )
    assert [m.song["videoId"] for m in pair.candidate.matches] == ["B"]
    assert (found.listed, found.unreadable, found.ambiguous, found.not_own_item) == (0, 0, 0, 0)


def test_saved_unknown_and_youtube_less_playlists_are_ignored(session: Session) -> None:
    def other(pid: str, artist: str, owned: bool | None, youtube: bool = True) -> PlaylistRead:
        tracks = [_atv("A", artist=artist)]
        return _read(pid, pid, tracks, _side(pid, "A") if youtube else None, owned)

    scan, found = _found(
        session,
        other("PLs", "AR2", False),
        other("PLu", "AR3", None),
        other("PLn", "AR4", True, youtube=False),
        artists={i: _page(_atv("B", artist=i)) for i in ("AR2", "AR3", "AR4")},
    )

    assert found.pairs == ()
    assert artist_ids(scan) == []


def test_artist_ids_list_each_artist_once_across_playlists(session: Session) -> None:
    scan, _ = _found(
        session,
        _mix(_atv("C", "Other", artist="AR2"), extra_youtube=("C",)),
        _read("PLb", "Two", [_atv("D", "Third")], _side("PLb", "D")),
    )

    assert artist_ids(scan) == ["AR1", "AR2"]


def test_a_dead_unchecked_or_not_self_backed_a_is_counted_not_a_finding(session: Session) -> None:
    _, found = _found(
        session,
        _read("PLd", "Dead", [_atv("A")], _side("PLd", ("A", False))),
        _read("PLu", "Unchecked", [_atv("A")], _side("PLu", ("A", None))),
        # YT Music shows A for another item (dead D; playable M, e.g. an MV
        # shown as its audio): moving A would mean moving that item.
        _read("PLr", "Rendered", [_filler("f0"), _atv("A")], _side("PLr", "f0", ("D", False))),
        _read("PLm", "Video", [_filler("f0"), _atv("A")], _side("PLm", "f0", "M")),
    )

    assert found.pairs == ()
    assert found.not_own_item == 4


def test_an_unreadable_artist_blocks_its_a(session: Session) -> None:
    _, found = _found(session, _mix(), artists={})

    assert found.pairs == ()
    assert found.unreadable == 1


def test_an_a_already_listed_is_counted(session: Session) -> None:
    _, found = _found(session, _mix(), artists={"AR1": _page(_atv("A"), _B)})

    assert found.pairs == ()
    assert found.listed == 1


def test_an_a_with_two_bs_is_only_counted(session: Session) -> None:
    _, found = _found(session, _mix(), artists={"AR1": _page(_B, _atv("B2", album="Other"))})

    assert found.pairs == ()
    assert found.ambiguous == 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _reported(session: Session, *reads: PlaylistRead, artists=None):
    scan, found = _found(session, *reads, artists=artists)
    return persist_report(session, scan.scan.id, found.pairs)


def test_a_clean_pair_is_write_eligible_with_its_ids_and_tracks(session: Session) -> None:
    diagnosis, carried = _reported(session, _mix())

    assert carried == 0
    assert (diagnosis.kind, diagnosis.playlist_scan_id) == (
        DIAGNOSIS_KIND_PLAYLIST_CANONICALIZE,
        load_scan(session).scan.id,
    )
    (item,) = diagnosis.items
    assert (item.issue_type, item.confidence, item.status) == (
        ISSUE_PLAYLIST_CANONICAL_VERSION,
        1.0,
        "open",
    )
    a = session.get(SnapshotItem, item.source_item_id)
    shown = session.get(SnapshotItem, item.related_item_id)
    assert (a.snapshot.source, a.video_id) == ("youtube_playlist", "A")
    assert (shown.snapshot.source, shown.video_id) == ("ytmusic_playlist", "A")
    assert item.source_track_id == a.track_id
    b = session.get(Track, item.related_track_id)
    assert (b.source, b.video_id) == ("ytmusic_liked_songs", "B")


def test_the_reason_names_playlist_position_a_b_artist_and_presence(session: Session) -> None:
    diagnosis, _ = _reported(session, _mix())

    (item,) = diagnosis.items
    assert item.reason == (
        "playlist 'Mix', position 2; "
        "A=A 'Song' (album 'Single', 200 s); "
        "B=B 'Song' (album 'Album', 200 s); "
        "artist 'Name AR1' lists B; B already in the playlist: no"
    )


def _cause(session: Session, *reads: PlaylistRead) -> str:
    diagnosis, _ = _reported(session, *reads)
    (item,) = diagnosis.items
    assert item.confidence == 0.5
    return item.reason.split("report-only: ")[1]


def test_a_version_keyword_makes_it_report_only(session: Session) -> None:
    assert _cause(session, _mix(album="Live at Hall")) == "version keyword 'Live' in A's album"


def test_a_listed_twice_makes_it_report_only(session: Session) -> None:
    cause = _cause(session, _mix(_atv("A"), extra_youtube=("A",)))
    assert cause == "A listed 2 times on the YouTube side, shown 2 times by YT Music"


def test_b_on_one_side_only_makes_it_report_only(session: Session) -> None:
    # YT Music shows B for a dead D: B is shown, but no item of B is listed.
    cause = _cause(session, _mix(_B, extra_youtube=(("D", False),)))
    assert cause == "B listed 0 times on the YouTube side, shown 1 times by YT Music"


def test_b_listed_more_than_once_makes_it_report_only(session: Session) -> None:
    cause = _cause(session, _mix(_B, _B, extra_youtube=("B", "B")))
    assert cause == "B listed 2 times on the YouTube side, shown 2 times by YT Music"


def test_b_already_in_the_playlist_once_is_write_eligible_and_named(session: Session) -> None:
    diagnosis, _ = _reported(session, _mix(_B, extra_youtube=("B",)))

    (item,) = diagnosis.items
    assert item.confidence == 1.0
    assert item.reason.endswith("B already in the playlist: yes")


def test_a_skip_carries_over_in_the_same_playlist_only(session: Session) -> None:
    reads = (_mix(), _read("PLb", "Two", [_atv("A")], _side("PLb", "A")))
    first, _ = _reported(session, *reads)
    by_playlist = {
        session.get(SnapshotItem, it.source_item_id).snapshot.playlist_id: it for it in first.items
    }
    by_playlist["PLb"].status = "skipped"
    session.flush()

    second, carried = _reported(session, *reads)

    assert carried == 1
    status = {
        session.get(SnapshotItem, it.source_item_id).snapshot.playlist_id: it.status
        for it in second.items
    }
    assert status == {"PLa": "open", "PLb": "skipped"}


def test_a_skip_never_carries_over_from_a_playlist_relinked_finding(session: Session) -> None:
    first, _ = _reported(session, _mix())
    (item,) = first.items
    relinked = Diagnosis(
        ytmusic_snapshot_id=None,
        youtube_snapshot_id=None,
        kind="playlists",
        playlist_scan_id=first.playlist_scan_id,
    )
    session.add(relinked)
    session.flush()
    session.add(
        DiagnosisItem(
            diagnosis_id=relinked.id,
            issue_type=ISSUE_PLAYLIST_RELINKED,
            confidence=1.0,
            reason="r",
            source_track_id=item.source_track_id,
            related_track_id=item.related_track_id,
            source_item_id=item.source_item_id,
            related_item_id=item.related_item_id,
            status="skipped",
        )
    )
    session.flush()

    second, carried = _reported(session, _mix())

    assert carried == 0
    assert [it.status for it in second.items] == ["open"]


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------


def _planned(session: Session, *reads: PlaylistRead, include_flagged: bool = False, artists=None):
    diagnosis, _ = _reported(session, *reads, artists=artists)
    return diagnosis, plan(session, diagnosis, include_flagged=include_flagged)


def test_only_open_write_eligible_items_become_actions(session: Session) -> None:
    diagnosis, (actions, states) = _planned(session, _mix())
    assert len(actions) == 1
    (item,) = diagnosis.items

    for status, confidence in (("skipped", 1.0), ("applied", 1.0), ("open", 0.5)):
        item.status, item.confidence = status, confidence
        session.flush()
        assert plan(session, diagnosis, include_flagged=False) == ([], {})


def test_an_action_takes_a_from_youtube_and_its_index_from_ytmusic(session: Session) -> None:
    # A deleted item YT Music doesn't show sits before A: the indices differ.
    read = _read("PLa", "Mix", [_atv("A")], _side("PLa", ("D", False), "A"))
    diagnosis, (actions, states) = _planned(session, read)

    (action,) = actions
    assert action.item_id == diagnosis.items[0].id
    assert (action.playlist_id, action.playlist_title) == ("PLa", "Mix")
    assert (action.a_index, action.a_playlist_item_id, action.a_video_id) == (1, "PLa-1", "A")
    assert (action.b_video_id, action.b_index, action.kind) == ("B", 0, "canonical_repoint")
    assert states == {
        "PLa": PlaylistState(youtube=(("PLa-0", "D"), ("PLa-1", "A")), ytmusic=("A",))
    }


def test_include_flagged_adds_a_keyword_only_finding_and_nothing_else(session: Session) -> None:
    reads = (
        _mix(album="Live at Hall"),  # keyword only
        # keyword and A listed twice
        _read(
            "PLb",
            "Two",
            [_atv("A", album="Live at Hall"), _atv("A", album="Live at Hall")],
            _side("PLb", "A", "A"),
        ),
        # B on one side only, no keyword
        _read("PLc", "Three", [_atv("A"), _B], _side("PLc", "A", ("D", False))),
    )
    diagnosis, (actions, _) = _planned(session, *reads)
    assert actions == []
    assert [it.confidence for it in diagnosis.items] == [0.5, 0.5, 0.5]

    flagged, _ = plan(session, diagnosis, include_flagged=True)

    assert [a.playlist_id for a in flagged] == ["PLa"]


def test_forecast_is_merge_when_b_is_in_the_playlist_or_an_earlier_action_puts_it_there(
    session: Session,
) -> None:
    reads = (
        _mix(_B, extra_youtube=("B",)),
        # Two A's of one recording sharing B: repoint, then merge.
        _read(
            "PLb",
            "Two",
            [_atv("A1"), _filler("f1"), _atv("A2")],
            _side("PLb", "A1", "f1", "A2"),
        ),
    )
    _, (actions, _) = _planned(session, *reads)

    assert [(a.playlist_id, a.a_video_id, a.kind) for a in actions] == [
        ("PLa", "A", "canonical_merge"),
        ("PLb", "A1", "canonical_repoint"),
        ("PLb", "A2", "canonical_merge"),
    ]


def test_order_is_smallest_playlist_then_playlist_id_then_position(session: Session) -> None:
    reads = (
        _read("PLz", "Z", [_atv("A1"), _atv("A2")], _side("PLz", "A1", "A2")),
        _read("PLy", "Y", [_atv("A3")], _side("PLy", "A3")),
        _read("PLx", "X", [_atv("A5"), _atv("A4")], _side("PLx", "A5", "A4")),
    )
    _, (actions, _) = _planned(session, *reads)

    assert [(a.playlist_id, a.a_index) for a in actions] == [
        ("PLy", 0),
        ("PLx", 0),
        ("PLx", 1),
        ("PLz", 0),
        ("PLz", 1),
    ]


def test_summarize_counts_kinds_and_estimates_quota(session: Session) -> None:
    reads = (
        _read(
            "PLb",
            "Two",
            [_atv("A1"), _filler("f1"), _atv("A2")],
            _side("PLb", "A1", "f1", "A2"),
        ),
    )
    _, (actions, states) = _planned(session, *reads)

    text = summarize(actions, states)

    assert "canonical_repoint: 1" in text and "canonical_merge: 1" in text
    assert "Playlists: 1" in text
    assert "merge removes one entry" in text
    assert "sort order" in text
    # One read of 3 items; a repoint 100 + reads of 4 and 3; a merge 50 + a read of 2.
    assert "Estimated YouTube quota: 154 units" in text
    assert "automatically sorted playlist" in text and "50 units" in text
