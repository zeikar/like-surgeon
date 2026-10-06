"""Heuristic music-candidate classifier tests."""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("Moonlit Harbor (Cover)", "cover"),
        ("Moonlit Harbor cover by Aria", "cover"),
        ("【歌ってみた】月灯りの港 / 架空太郎", "歌ってみた"),
        ("【弾いてみた】月灯りの港", "弾いてみた"),
        ("【オリジナル曲】月灯りの港", "オリジナル曲"),
        ("Moonlit Harbor (Original Song)", "original song"),
        ("Harbor Quest OST - Moonlit Harbor", "ost"),
        ("Harbor Quest Soundtrack: Moonlit Harbor", "soundtrack"),
        ("Moonlit Harbor BGM", "bgm"),
        ("Moonlit Harbor Theme Song", "theme song"),
        ("Moonlit Harbor (Remastered)", "remaster"),
        ("Moonlit Harbor Arrangement", "arrange"),
        ("Moonlit Harbor (Rendition)", "rendition"),
        ("Moonlit Harbor (Fake Artist Remix)", "remix"),
        ("Moonlit Harbor Mashup", "mashup"),
        ("Moonlit Harbor Piano ver.", "ver"),
        ("Moonlit Harbor (Instrumental)", "instrumental"),
        ("Moonlit Harbor Vocaloid demo", "vocaloid"),
        ("月灯りの港 ボカロ", "vocaloid"),
        ("Moonlit Harbor feat. Mira", "feat"),
        ("Moonlit Harbor Nightcore", "nightcore"),
        ("Moonlit Harbor (sped up)", "sped up"),
        ("Moonlit Harbor (slowed)", "slowed"),
        ("가상게임 로그인bgm", "bgm"),
        ("架空の街テーマbgm", "bgm"),
        ("夜明けcover", "cover"),
        ("【架空太郎】夕焼け坂 | 歌い手活動三周年", "歌い手"),
    ],
)
def test_cover_ost_remix_families_are_music(title, label):
    res = _c(title=title, channel="Plain Channel")
    assert res.is_music_candidate is True
    assert label in res.reason.lower()


@pytest.mark.parametrize(
    "title",
    [
        "How to design a book cover - tutorial",
        "Game remaster review",
        "Top 10 game soundtracks explained",
        "Studio vlog: recording the cover",
        "Flower arrangement how-to",
        "How  to cover a chair",
        "Remastered documentary trailer",
        "Soundtrack podcast episode 3",
    ],
)
def test_music_words_in_non_music_titles_are_not_music(title):
    assert _c(title=title, channel="Plain Channel").is_music_candidate is False


def test_glued_latin_negative_still_fires():
    res = _c(title="架空の街の話podcast", channel="Plain Channel")
    assert res.is_music_candidate is False
    assert "podcast" in res.reason.lower()


@pytest.mark.parametrize("title", ["Moonlit Harbor remixes", "Moonlit Harbor discovered"])
def test_latin_token_inside_longer_ascii_word_does_not_match(title):
    assert _c(title=title, channel="Plain Channel").score == 0
