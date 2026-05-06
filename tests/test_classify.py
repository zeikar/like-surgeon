"""Heuristic music-candidate classifier tests."""

from __future__ import annotations

from likesurgeon.classify import Classification, classify


def _c(
    title: str = "",
    channel: str | None = None,
    description: str | None = None,
) -> Classification:
    return classify(title=title, channel=channel, description=description)


def test_official_music_video_is_music():
    res = _c(title="Song Name (Official Music Video)", channel="ArtistVEVO")
    assert res.is_music_candidate is True
    assert res.score >= 2
    assert "official music video" in res.reason.lower()


def test_topic_channel_is_music():
    res = _c(title="Song Name", channel="Artist - Topic")
    assert res.is_music_candidate is True
    assert "topic" in res.reason.lower()


def test_provided_to_youtube_in_description_is_music():
    res = _c(
        title="Song Name",
        channel="Some Channel",
        description="Provided to YouTube by SomeLabel\n\nSong · Artist · Album",
    )
    assert res.is_music_candidate is True


def test_artist_dash_title_pattern_is_music():
    res = _c(title="The Beatles - Hey Jude", channel="Some Channel")
    assert res.is_music_candidate is True
    assert "artist - title" in res.reason.lower()


def test_lyrics_video_is_music():
    res = _c(title="Song Name (Lyrics)", channel="LyricsChannel")
    assert res.is_music_candidate is True


def test_visualizer_is_music():
    res = _c(title="Song Name | Visualizer", channel="Whoever")
    assert res.is_music_candidate is True


def test_vlog_title_is_not_music():
    res = _c(title="Daily vlog #42", channel="VlogChannel")
    assert res.is_music_candidate is False
    assert "vlog" in res.reason.lower()


def test_tutorial_is_not_music():
    res = _c(title="How to bake bread - Tutorial", channel="CookChannel")
    assert res.is_music_candidate is False


def test_gameplay_is_not_music():
    res = _c(title="Elden Ring boss gameplay walkthrough", channel="GamerName")
    assert res.is_music_candidate is False


def test_negative_outweighs_weak_positive():
    """A title with both 'audio' (weak +) and 'reaction' (strong -) is NOT music."""
    res = _c(title="My reaction to the new audio drama", channel="ReactionChannel")
    assert res.is_music_candidate is False


def test_plain_unrelated_title_is_not_music():
    res = _c(title="Why I moved out of NYC", channel="LifestyleChannel")
    assert res.is_music_candidate is False
    assert res.score == 0


def test_classification_is_pure():
    """Same input → same output, no time/global state."""
    a = _c(title="Song (Official MV)", channel="ArtistVEVO")
    b = _c(title="Song (Official MV)", channel="ArtistVEVO")
    assert a == b
