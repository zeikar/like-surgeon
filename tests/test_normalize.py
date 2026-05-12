from likesurgeon.normalize import (
    canonical_key,
    normalize_artists,
    normalize_for_match,
    normalize_title,
)


def test_normalize_title_strips_official_music_video():
    assert normalize_title("Song Name (Official Music Video)") == "song name"


def test_normalize_title_strips_official_audio():
    assert normalize_title("Song Name [Official Audio]") == "song name"


def test_normalize_title_strips_lyrics_bracket():
    assert normalize_title("Song Name (Lyrics)") == "song name"


def test_normalize_title_strips_mv_suffix():
    assert normalize_title("Song Name - MV") == "song name"


def test_normalize_title_strips_visualizer():
    assert normalize_title("Song Name (Visualizer)") == "song name"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("  Song   Name  ") == "song name"


def test_normalize_title_preserves_non_noise_brackets():
    # Year tag is not a noise word — keep it.
    assert normalize_title("Song Name (2024)") == "song name (2024)"


def test_normalize_artists_lowercases_and_sorts():
    assert normalize_artists(["BTS", "Coldplay"]) == "bts, coldplay"
    assert normalize_artists(["Coldplay", "BTS"]) == "bts, coldplay"


def test_normalize_artists_drops_empty():
    assert normalize_artists(["", "  ", "Artist"]) == "artist"


def test_canonical_key_matches_across_decoration():
    a = canonical_key("Song (Official Audio)", ["Artist"])
    b = canonical_key("song", ["artist"])
    assert a == b


def test_canonical_key_distinguishes_different_songs():
    a = canonical_key("Song A", ["Artist"])
    b = canonical_key("Song B", ["Artist"])
    assert a != b


def test_normalize_for_match_unifies_tilde_variants():
    result_wave = normalize_for_match("えがお、み〜っけた！")  # U+301C wave dash
    result_full = normalize_for_match("えがお、み～っけた！")  # U+FF5E fullwidth tilde
    result_ascii = normalize_for_match("えがお、み~っけた！")  # ASCII tilde
    assert result_wave == result_full == result_ascii
    assert "~" in result_wave
    assert "〜" not in result_wave
    assert "～" not in result_wave


def test_normalize_for_match_nfkc_fullwidth_alpha():
    assert normalize_for_match("ＡＢＣ") == "abc"


def test_normalize_for_match_strip_and_casefold():
    assert normalize_for_match("  Hello  ") == "hello"


def test_normalize_for_match_preserves_kana():
    assert normalize_for_match("カタカナ") == "カタカナ"


def test_normalize_for_match_empty_string():
    assert normalize_for_match("") == ""
