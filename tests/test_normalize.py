from likesurgeon.normalize import canonical_key, normalize_artists, normalize_title


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
