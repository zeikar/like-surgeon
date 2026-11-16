"""Tests for canonicalize discovery — pure, fictional data only."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from likesurgeon.canonicalize import (
    DiscoverySummary,
    artist_ids,
    find_candidates,
    flagged_keywords,
    persist_report,
    plan,
    summarize,
    version_keyword,
)
from likesurgeon.diagnosis import (
    ISSUE_CANONICAL_AMBIGUOUS,
    ISSUE_CANONICAL_VERSION,
    diagnosis_items,
    latest_diagnosis,
)
from likesurgeon.models import Diagnosis, Snapshot, Track
from likesurgeon.snapshot import YTMUSIC_LIKED_SONGS, create_snapshot, get_snapshot_items
from likesurgeon.sync import resolve_video_ids
from likesurgeon.ytmusic_client import ArtistSongs

from .sync_items import canonical_merge, canonical_repoint, make_diagnosis, pair

ATV = "MUSIC_VIDEO_TYPE_ATV"
OMV = "MUSIC_VIDEO_TYPE_OMV"


def song(
    video_id: str,
    title: str = "Paper Lantern",
    duration: int | None = 200,
    *,
    album: str | None = "Paper Lantern - Single",
    artists: tuple[str, ...] = ("ch_art",),
    video_type: str = ATV,
) -> dict[str, Any]:
    """A liked / playlist track dict as ytmusicapi returns it."""
    raw: dict[str, Any] = {
        "videoId": video_id,
        "title": title,
        "album": None if album is None else {"name": album, "id": f"alb_{video_id}"},
        "artists": [{"name": f"Name {a}", "id": a} for a in artists],
        "videoType": video_type,
        "likeStatus": "INDIFFERENT",
    }
    if duration is not None:
        raw["duration_seconds"] = duration
    return raw


def page(*songs: dict[str, Any], top: tuple[str, ...] = ()) -> ArtistSongs:
    return ArtistSongs(top_video_ids=top, songs=tuple(songs))


def _b(**kw: Any) -> dict[str, Any]:
    return song("vid_b", album="Night Market", **kw)


# --- artist_ids ---------------------------------------------------------------


def test_artist_ids_distinct_first_seen_order_skipping_missing_ids():
    liked = [
        song("v1", artists=("ch_2", "ch_1")),
        song("v2", artists=("ch_1",)),
        {**song("v3"), "artists": [{"name": "No Channel", "id": None}]},
        song("v4", artists=("ch_3",)),
    ]
    assert artist_ids(liked) == ["ch_2", "ch_1", "ch_3"]


# --- candidate rule -----------------------------------------------------------


def test_single_match_is_one_candidate():
    a = song("vid_a", album="Paper Lantern - Single")
    b = _b(duration=201)
    candidates, summary = find_candidates([a], {"ch_art": page(b)})

    assert len(candidates) == 1
    c = candidates[0]
    assert c.a is a
    assert [(m.song["videoId"], m.artist_id) for m in c.matches] == [("vid_b", "ch_art")]
    assert c.matches[0].in_top is False
    assert c.matches[0].liked is False
    assert c.shown == 1
    assert c.keyword is None
    assert c.ambiguous is False
    assert c.report_only_cause is None
    assert summary == DiscoverySummary(songs=1, listed=0, candidates=1, ambiguous=0, unreadable=0)


def test_title_key_ignores_fullwidth_case_and_extra_spaces():
    a = song("vid_a", title="Ｐａｐｅｒ  LANTERN ")
    b = _b(title="paper lantern")
    candidates, _ = find_candidates([a], {"ch_art": page(b)})
    assert [m.song["videoId"] for m in candidates[0].matches] == ["vid_b"]


def test_instrumental_title_is_not_a_match():
    a = song("vid_a", title="Paper Lantern", duration=200)
    inst = _b(title="Paper Lantern (instrumental)", duration=201)
    candidates, _ = find_candidates([a], {"ch_art": page(inst)})
    assert candidates == []


def test_two_seconds_apart_is_not_a_match():
    a = song("vid_a", duration=200)
    candidates, _ = find_candidates([a], {"ch_art": page(_b(duration=202))})
    assert candidates == []


def test_b_without_duration_is_not_a_match():
    a = song("vid_a")
    candidates, _ = find_candidates([a], {"ch_art": page(_b(duration=None))})
    assert candidates == []


def test_a_in_the_songs_list_counts_as_listed():
    a = song("vid_a")
    candidates, summary = find_candidates([a], {"ch_art": page(song("vid_a"), _b())})
    assert candidates == []
    assert summary.listed == 1


def test_a_in_top_ids_counts_as_listed():
    a = song("vid_a")
    candidates, summary = find_candidates([a], {"ch_art": page(_b(), top=("vid_a",))})
    assert candidates == []
    assert summary.listed == 1


def test_a_listed_under_its_second_artist_counts_as_listed():
    a = song("vid_a", artists=("ch_1", "ch_2"))
    artists = {"ch_1": page(_b()), "ch_2": page(song("vid_a"))}
    candidates, summary = find_candidates([a], artists)
    assert candidates == []
    assert summary.listed == 1


def test_a_not_atv_is_skipped():
    a = song("vid_a", video_type=OMV)
    candidates, summary = find_candidates([a], {"ch_art": page(_b())})
    assert candidates == []
    assert summary.listed == 0


def test_b_not_atv_is_not_a_match():
    a = song("vid_a")
    candidates, _ = find_candidates([a], {"ch_art": page(_b(video_type=OMV))})
    assert candidates == []


def test_a_without_artist_ids_is_skipped():
    a = {**song("vid_a"), "artists": [{"name": "No Channel", "id": None}]}
    candidates, _ = find_candidates([a], {"ch_art": page(_b())})
    assert candidates == []


def test_a_without_duration_is_skipped():
    a = song("vid_a", duration=None)
    candidates, _ = find_candidates([a], {"ch_art": page(_b())})
    assert candidates == []


def test_artist_without_songs_list_yields_nothing():
    a = song("vid_a")
    candidates, summary = find_candidates([a], {"ch_art": None})
    assert candidates == []
    assert summary.unreadable == 0


def test_artist_without_songs_list_does_not_block_another_artists_match():
    a = song("vid_a", artists=("ch_plain", "ch_art"))
    candidates, _ = find_candidates([a], {"ch_plain": None, "ch_art": page(_b())})
    assert [m.artist_id for m in candidates[0].matches] == ["ch_art"]


# --- edges --------------------------------------------------------------------


def test_two_matching_bs_are_ambiguous():
    a = song("vid_a")
    b1 = song("vid_b1", album="Night Market")
    b2 = song("vid_b2", album="Best Of", duration=199)
    candidates, summary = find_candidates([a], {"ch_art": page(b1, b2)})

    assert len(candidates) == 1
    assert candidates[0].ambiguous is True
    assert [m.song["videoId"] for m in candidates[0].matches] == ["vid_b1", "vid_b2"]
    assert (summary.candidates, summary.ambiguous) == (0, 1)


def test_same_b_through_two_artists_counts_once():
    a = song("vid_a", artists=("ch_1", "ch_2"))
    b = _b(artists=("ch_1", "ch_2"))
    artists = {"ch_1": page(b), "ch_2": page(b, top=("vid_b",))}
    candidates, _ = find_candidates([a], artists)

    (c,) = candidates
    assert c.ambiguous is False
    assert [(m.song["videoId"], m.artist_id) for m in c.matches] == [("vid_b", "ch_1")]
    # Top songs under either of A's artists counts.
    assert c.matches[0].in_top is True


def test_unreadable_artist_blocks_the_finding():
    a = song("vid_a", artists=("ch_ok", "ch_err"))
    candidates, summary = find_candidates([a], {"ch_ok": page(_b())})
    assert candidates == []
    assert summary.unreadable == 1


def test_b_in_top_ids_is_flagged():
    a = song("vid_a")
    candidates, _ = find_candidates([a], {"ch_art": page(_b(), top=("vid_b",))})
    assert candidates[0].matches[0].in_top is True


def test_b_already_liked_is_flagged():
    a = song("vid_a")
    b = _b()
    candidates, summary = find_candidates([a, b], {"ch_art": page(b)})
    (c,) = candidates
    assert c.a["videoId"] == "vid_a"
    assert c.matches[0].liked is True
    # B itself is listed.
    assert summary.listed == 1


def test_a_shown_twice_is_one_report_only_candidate():
    a = song("vid_a")
    candidates, summary = find_candidates([a, song("vid_x", artists=()), a], {"ch_art": page(_b())})
    (c,) = candidates
    assert c.shown == 2
    assert c.report_only_cause == "A shown 2 times in Liked songs"
    assert summary.songs == 2


def test_summary_counts():
    liked = [
        song("listed_1", title="Tin Moon"),
        song("listed_2", title="Salt Road"),
        song("vid_a"),
        song("vid_amb", title="Glass River"),
        song("vid_blocked", title="Iron Bell", artists=("ch_art", "ch_err")),
        song("omv", video_type=OMV),
        song("nothing", title="No Twin"),
    ]
    artists = {
        "ch_art": page(
            song("listed_1", title="Tin Moon"),
            _b(),
            song("gr_1", title="Glass River"),
            song("gr_2", title="Glass River"),
            top=("listed_2",),
        )
    }
    _, summary = find_candidates(liked, artists)
    assert summary == DiscoverySummary(songs=7, listed=2, candidates=1, ambiguous=1, unreadable=1)


# --- version-keyword net ------------------------------------------------------


@pytest.mark.parametrize(
    ("album", "keyword"),
    [
        ("Paper Lantern (Artist Version)", "Version"),
        ("Paper Lantern (TV ver.)", "Ver."),
        ("Live at the Pier", "Live"),
        ("LIVE2019", "Live"),
        ("Best Live! collection", "Live"),
        ("Lanterns - The REMIX album", "Remix"),
        ("acoustic sessions", "Acoustic"),
        ("Paper Lantern [Instrumental]", "Instrumental"),
        ("Paper Lantern Off Vocal", "Off Vocal"),
        ("Paper Lantern (tv size)", "TV Size"),
        ("ＬＩＶＥ ＴＯＵＲ", "Live"),
        ("Paper Lantern Ver．1", "Ver."),
        ("Paper Lantern (Off-Vocal)", "Off Vocal"),
        ("Paper Lantern Off  Vocal", "Off Vocal"),
        ("Paper Lantern TV-Size", "TV Size"),
        ("Paper Lantern TVsize", "TV Size"),
    ],
)
def test_version_keyword_in_album_makes_report_only(album, keyword):
    assert version_keyword(album) == keyword

    a = song("vid_a", album=album)
    (c,) = find_candidates([a], {"ch_art": page(_b())})[0]
    assert c.keyword == keyword
    assert c.report_only_cause == f"version keyword '{keyword}' in A's album"


@pytest.mark.parametrize(
    "album",
    [
        "LoveLive! Sunshine!! Compilation",
        "Alive",
        "Versions of Me",
        "Paper Lantern - Single",
    ],
)
def test_keyword_embedded_in_a_longer_word_stays_eligible(album):
    assert version_keyword(album) is None

    a = song("vid_a", album=album)
    (c,) = find_candidates([a], {"ch_art": page(_b())})[0]
    assert c.keyword is None
    assert c.report_only_cause is None


def test_album_missing_has_no_keyword():
    a = song("vid_a", album=None)
    (c,) = find_candidates([a], {"ch_art": page(_b())})[0]
    assert c.keyword is None


def test_both_causes_are_reported():
    a = song("vid_a", album="Live at the Pier")
    (c,) = find_candidates([a, a], {"ch_art": page(_b())})[0]
    assert c.report_only_cause == (
        "version keyword 'Live' in A's album; A shown 2 times in Liked songs"
    )


# --- performers ---------------------------------------------------------------


def _duet(video_id: str, *artists: tuple[str, str], album: str = "Night Market") -> dict[str, Any]:
    """``song`` credited to ``artists`` as (id, name) pairs."""
    return {
        **song(video_id, album=album),
        "artists": [{"name": name, "id": i} for i, name in artists],
    }


_TENOR, _SOPRANO, _ALTO = ("ch_tenor", "Tenor"), ("ch_soprano", "Soprano"), ("ch_alto", "Alto")


def test_lone_b_by_other_performers_is_report_only():
    a = _duet("vid_a", _TENOR, _SOPRANO, album="Paper Lantern - Single")
    b = _duet("vid_b", _ALTO, _TENOR)
    candidates, summary = find_candidates([a], {"ch_tenor": page(b), "ch_soprano": page()})

    (c,) = candidates
    assert c.ambiguous is False
    assert c.report_only_cause == (
        "different performers: A by 'Tenor · Soprano', B by 'Alto · Tenor'"
    )
    assert summary.candidates == 1


def test_same_artist_ids_under_other_names_stay_write_eligible():
    a = _duet("vid_a", ("ch_k", "Kaze"), _TENOR, album="Paper Lantern - Single")
    b = _duet("vid_b", _TENOR, ("ch_k", "風"))
    (c,) = find_candidates([a], {"ch_k": page(b), "ch_tenor": page()})[0]
    assert c.report_only_cause is None


def test_b_without_artist_ids_is_report_only():
    a = song("vid_a")
    b = {**_b(), "artists": [{"name": "Name ch_art", "id": None}]}
    (c,) = find_candidates([a], {"ch_art": page(b)})[0]
    assert c.report_only_cause == ("different performers: A by 'Name ch_art', B by 'Name ch_art'")


def test_several_bs_stay_ambiguous_even_if_only_one_has_as_performers():
    a = _duet("vid_a", _TENOR, _SOPRANO)
    same = _duet("vid_b1", _TENOR, _SOPRANO)
    other = _duet("vid_b2", _ALTO, _TENOR)
    candidates, summary = find_candidates(
        [a], {"ch_tenor": page(same, other), "ch_soprano": page()}
    )

    (c,) = candidates
    assert c.ambiguous is True
    assert c.report_only_cause is None
    assert (summary.candidates, summary.ambiguous) == (0, 1)


def test_unplayable_list_row_without_video_id_is_not_a_match():
    a = song("vid_a")
    unplayable = {**_b(), "videoId": None}
    candidates, _ = find_candidates([a], {"ch_art": page(unplayable)})
    assert candidates == []


def test_same_b_twice_in_one_list_is_one_match():
    a = song("vid_a")
    candidates, summary = find_candidates([a], {"ch_art": page(_b(), _b())})
    (c,) = candidates
    assert c.ambiguous is False
    assert [m.song["videoId"] for m in c.matches] == ["vid_b"]
    assert summary.candidates == 1


def test_liked_rows_without_video_id_are_ignored():
    unplayable = {**song("vid_x", title="Tin Moon"), "videoId": None}
    a = song("vid_a")
    # A None in the list must not make the unplayable row "listed".
    artists = {"ch_art": page({**song("vid_y", title="Tin Moon"), "videoId": None}, _b())}
    candidates, summary = find_candidates([unplayable, a, unplayable], artists)
    assert [(c.a["videoId"], c.shown) for c in candidates] == [("vid_a", 1)]
    assert summary == DiscoverySummary(songs=1, listed=0, candidates=1, ambiguous=0, unreadable=0)


# --- persist_report -----------------------------------------------------------


def _report(session: Session, liked: list[dict[str, Any]], pages: dict[str, ArtistSongs]):
    snap = create_snapshot(session, YTMUSIC_LIKED_SONGS, liked)
    candidates, _ = find_candidates(liked, pages)
    return snap, candidates


def test_persist_report_writes_canonicalize_diagnosis_and_items(session: Session):
    a = song("vid_a", album="Paper Lantern - Single")
    b = _b(duration=201)
    snap, candidates = _report(session, [a], {"ch_art": page(b, top=("vid_b",))})
    snapshots_before = session.scalar(select(Snapshot.id).order_by(Snapshot.id.desc()))

    diagnosis, carried = persist_report(session, snap.id, candidates)

    assert carried == 0
    assert session.scalar(select(Snapshot.id).order_by(Snapshot.id.desc())) == snapshots_before
    assert diagnosis.kind == "canonicalize"
    assert diagnosis.ytmusic_snapshot_id == snap.id
    assert diagnosis.youtube_snapshot_id is None
    (item,) = diagnosis_items(session, diagnosis.id)
    assert item.issue_type == "canonical_version"
    assert item.confidence == 1.0
    assert item.status == "open"
    track_a = session.get(Track, item.source_track_id)
    track_b = session.get(Track, item.related_track_id)
    assert (track_a.video_id, track_b.video_id) == ("vid_a", "vid_b")
    assert track_b.source == YTMUSIC_LIKED_SONGS
    for expected in (
        "vid_a",
        "vid_b",
        "Paper Lantern",
        "Paper Lantern - Single",
        "Night Market",
        "200",
        "201",
        "Name ch_art",
        "B in Top songs: yes",
        "B already liked: no",
    ):
        assert expected in item.reason
    assert "report-only" not in item.reason


def test_persist_report_report_only_cause_halves_confidence(session: Session):
    a = song("vid_a", album="Paper Lantern (Live)")
    snap, candidates = _report(session, [a], {"ch_art": page(_b())})

    diagnosis, _ = persist_report(session, snap.id, candidates)

    (item,) = diagnosis_items(session, diagnosis.id)
    assert item.confidence == 0.5
    assert "version keyword 'Live'" in item.reason


def test_persist_report_different_performers_is_report_only(session: Session):
    a = _duet("vid_a", _TENOR, _SOPRANO, album="Paper Lantern - Single")
    pages = {"ch_tenor": page(_duet("vid_b", _ALTO, _TENOR)), "ch_soprano": page()}
    snap, candidates = _report(session, [a], pages)

    diagnosis, _ = persist_report(session, snap.id, candidates)

    (item,) = diagnosis_items(session, diagnosis.id)
    assert (item.issue_type, item.confidence) == (ISSUE_CANONICAL_VERSION, 0.5)
    assert item.reason.endswith(
        "report-only: different performers: A by 'Tenor · Soprano', B by 'Alto · Tenor'"
    )


def test_persist_report_b_already_liked_is_in_reason(session: Session):
    a = song("vid_a")
    b = _b()
    snap, candidates = _report(session, [a, b], {"ch_art": page(b)})

    diagnosis, _ = persist_report(session, snap.id, candidates)

    (item,) = diagnosis_items(session, diagnosis.id)
    assert "B already liked: yes" in item.reason
    # B was already in the snapshot, so its Track is reused.
    assert len(session.scalars(select(Track).where(Track.video_id == "vid_b")).all()) == 1


def test_persist_report_ambiguous_lists_every_b(session: Session):
    a = song("vid_a")
    b1 = song("vid_b1", album="Night Market")
    b2 = song("vid_b2", album="Evening Market")
    snap, candidates = _report(session, [a], {"ch_art": page(b1, b2)})

    diagnosis, _ = persist_report(session, snap.id, candidates)

    (item,) = diagnosis_items(session, diagnosis.id)
    assert item.issue_type == "canonical_ambiguous"
    assert item.confidence == 0.5
    assert item.related_track_id is None
    assert item.status == "open"
    assert session.get(Track, item.source_track_id).video_id == "vid_a"
    assert "vid_b1" in item.reason and "vid_b2" in item.reason


def test_persist_report_keeps_skipped_across_reports(session: Session):
    a = song("vid_a")
    snap, candidates = _report(session, [a], {"ch_art": page(_b())})
    first, _ = persist_report(session, snap.id, candidates)
    diagnosis_items(session, first.id)[0].status = "skipped"
    session.flush()

    snap2, candidates2 = _report(session, [a], {"ch_art": page(_b())})
    second, carried = persist_report(session, snap2.id, candidates2)

    assert carried == 1
    assert [i.status for i in diagnosis_items(session, second.id)] == ["skipped"]


def test_persist_report_leaves_latest_alignment_diagnosis(session: Session):
    alignment = Diagnosis()
    session.add(alignment)
    session.flush()
    a = song("vid_a")
    snap, candidates = _report(session, [a], {"ch_art": page(_b())})
    report, _ = persist_report(session, snap.id, candidates)

    assert latest_diagnosis(session).id == alignment.id
    assert latest_diagnosis(session, "canonicalize").id == report.id
    assert session.get(Diagnosis, report.id).kind == "canonicalize"


# --- plan / summarize -----------------------------------------------------------


def _vids(items, session: Session) -> dict[int, str]:
    return resolve_video_ids(session, list(items))


def _canon(session, diag, a="A", b="B", **kw):
    return pair(session, diag, ISSUE_CANONICAL_VERSION, a=a, b=b, **kw)


def test_plan_open_full_confidence_becomes_an_action(session: Session):
    item = _canon(session, make_diagnosis(session))
    actions = plan(
        [item], _vids([item], session), lm_position={item.source_track_id: 3}, liked_video_ids=set()
    )
    assert actions == [canonical_repoint(item)]


def test_plan_drops_everything_else(session: Session):
    diag = make_diagnosis(session)
    items = [
        _canon(session, diag, a="A1", b="B1", confidence=0.5),
        pair(session, diag, ISSUE_CANONICAL_AMBIGUOUS, a="A2", b="B2"),
        _canon(session, diag, a="A3", b="B3", status="applied"),
        _canon(session, diag, a="A4", b="B4", status="skipped"),
    ]
    assert plan(items, _vids(items, session), lm_position={}, liked_video_ids=set()) == []


def test_plan_oldest_first(session: Session):
    diag = make_diagnosis(session)
    new = _canon(session, diag, a="A1", b="B1")
    old = _canon(session, diag, a="A2", b="B2")
    pos = {new.source_track_id: 1, old.source_track_id: 9}
    actions = plan([new, old], _vids([new, old], session), lm_position=pos, liked_video_ids=set())
    assert actions == [canonical_repoint(old, "A2", "B2"), canonical_repoint(new, "A1", "B1")]


def test_plan_forecasts_merge_when_b_is_liked(session: Session):
    item = _canon(session, make_diagnosis(session))
    actions = plan([item], _vids([item], session), lm_position={}, liked_video_ids={"B", "other"})
    assert actions == [canonical_merge(item)]


def test_plan_two_as_sharing_one_b_repoint_then_merge(session: Session):
    diag = make_diagnosis(session)
    first = _canon(session, diag, a="A1", b="B")
    second = _canon(session, diag, a="A2", b="B")
    pos = {first.source_track_id: 9, second.source_track_id: 1}
    actions = plan(
        [second, first], _vids([first, second], session), lm_position=pos, liked_video_ids=set()
    )
    assert actions == [canonical_repoint(first, "A1", "B"), canonical_merge(second, "A2", "B")]


def _plan_report(session: Session, liked, pages, *, include_flagged: bool):
    """Plan a report built from ``liked`` the way ``canonicalize --apply`` does.
    Returns the actions and each finding as (issue type, confidence)."""
    snap, candidates = _report(session, liked, pages)
    diagnosis, _ = persist_report(session, snap.id, candidates)
    items = diagnosis_items(session, diagnosis.id)
    lm = get_snapshot_items(session, snap.id)
    actions = plan(
        items,
        _vids(items, session),
        lm_position={it.track_id: it.position for it in lm},
        liked_video_ids={it.video_id for it in lm if it.video_id},
        flagged=flagged_keywords(session, items, lm) if include_flagged else None,
    )
    return actions, [(it.issue_type, it.confidence) for it in items]


def test_plan_never_takes_different_performers_even_with_a_keyword(session: Session):
    pages = {
        "ch_tenor": page(
            _duet("vid_b", _ALTO, _TENOR),
            _duet("vid_d", _ALTO, _TENOR, album="Harbor Lights") | {"title": "Tin Moon"},
        ),
        "ch_soprano": page(),
    }
    liked = [
        _duet("vid_a", _TENOR, _SOPRANO, album="Paper Lantern - Single"),
        _duet("vid_c", _TENOR, _SOPRANO, album="Live at the Pier") | {"title": "Tin Moon"},
    ]

    assert _plan_report(session, liked, pages, include_flagged=True) == (
        [],
        [(ISSUE_CANONICAL_VERSION, 0.5)] * 2,
    )


def test_plan_keyword_only_finding_needs_include_flagged(session: Session):
    a = song("vid_a", album="LoveLive! Best Album Best Live! collection")
    pages = {"ch_art": page(_b())}

    assert _plan_report(session, [a], pages, include_flagged=False) == (
        [],
        [(ISSUE_CANONICAL_VERSION, 0.5)],
    )
    (action,), _ = _plan_report(session, [a], pages, include_flagged=True)
    assert (action.kind, action.a_video_id, action.b_video_id) == (
        "canonical_repoint",
        "vid_a",
        "vid_b",
    )


def test_plan_keyword_finding_by_one_artist_under_two_names_is_flagged(session: Session):
    a = _duet("vid_a", ("ch_k", "Kaze"), album="Live at the Pier")
    pages = {"ch_k": page(_duet("vid_b", ("ch_k", "風")))}

    (action,), _ = _plan_report(session, [a], pages, include_flagged=True)
    assert (action.a_video_id, action.b_video_id) == ("vid_a", "vid_b")


def test_plan_flagged_excludes_an_a_shown_twice(session: Session):
    twice = song("vid_a", album="Paper Lantern - Single")
    both = song("vid_c", title="Tin Moon", album="Live at the Pier")
    pages = {"ch_art": page(_b(), song("vid_d", title="Tin Moon", album="Night Market"))}

    assert _plan_report(session, [twice, both, twice, both], pages, include_flagged=True) == (
        [],
        [(ISSUE_CANONICAL_VERSION, 0.5)] * 2,
    )


def test_plan_flagged_excludes_ambiguous(session: Session):
    a = song("vid_a", album="Live at the Pier")
    pages = {"ch_art": page(_b(), song("vid_b2", album="Harbor Lights"))}

    assert _plan_report(session, [a], pages, include_flagged=True) == (
        [],
        [(ISSUE_CANONICAL_AMBIGUOUS, 0.5)],
    )


def test_summarize_counts_quota_and_notes(session: Session):
    diag = make_diagnosis(session)
    r = _canon(session, diag, a="A1", b="B1")
    m = _canon(session, diag, a="A2", b="B2")
    text = summarize([canonical_repoint(r), canonical_merge(m)])
    assert "canonical_repoint: 1" in text
    assert "canonical_merge: 1" in text
    assert "157 units" in text
    assert "moves B to the top of Liked videos and Liked songs" in text
    assert "removes one entry from Liked songs" in text
    assert "WARNING" not in text


def test_summarize_only_merges_has_no_repoint_note(session: Session):
    m = _canon(session, make_diagnosis(session))
    text = summarize([canonical_merge(m)])
    assert "53 units" in text
    assert "moves B" not in text


def test_summarize_warns_over_daily_quota(session: Session):
    item = _canon(session, make_diagnosis(session))
    text = summarize([canonical_repoint(item)] * 97)  # 97 * 104 > 10000
    assert "WARNING: exceeds the default daily quota" in text
