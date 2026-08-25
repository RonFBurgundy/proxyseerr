from proxyseerr import namespace as ns

OFFSET = 1_000_000


def test_encode_decode_round_trip():
    assert ns.encode_id(42, OFFSET) == 1_000_042
    assert ns.decode_id(1_000_042, OFFSET) == ("anime", 42)
    assert ns.decode_id(42, OFFSET) == ("english", 42)


def test_zero_and_non_int_ids_pass_through():
    assert ns.encode_id(0, OFFSET) == 0
    assert ns.encode_id(None, OFFSET) is None
    assert ns.encode_id(True, OFFSET) is True
    assert ns.decode_id("tvdb:123", OFFSET) == ("english", "tvdb:123")


def test_encode_series_shifts_only_internal_ids():
    series = {
        "id": 7,
        "tvdbId": 81797,
        "tmdbId": 12345,
        "qualityProfileId": 4,
        "languageProfileId": 2,
        "tags": [1, 3],
        "title": "One Piece",
    }
    encoded = ns.encode_series(series, OFFSET)
    assert encoded["id"] == 1_000_007
    assert encoded["qualityProfileId"] == 1_000_004
    assert encoded["languageProfileId"] == 1_000_002
    assert encoded["tags"] == [1_000_001, 1_000_003]
    # External metadata IDs must survive untouched or Seerr loses the match.
    assert encoded["tvdbId"] == 81797
    assert encoded["tmdbId"] == 12345
    assert series["id"] == 7  # input not mutated


def test_decode_series_is_inverse_of_encode():
    series = {"id": 7, "qualityProfileId": 4, "tags": [1]}
    assert ns.decode_series(ns.encode_series(series, OFFSET), OFFSET) == series


def test_prefix_labels_only_touches_name():
    items = [{"id": 1, "name": "HD-1080p"}, {"id": 2, "path": "/data/media/anime"}]
    prefixed = ns.prefix_labels(items, "[Anime] ")
    assert prefixed[0]["name"] == "[Anime] HD-1080p"
    assert prefixed[1]["path"] == "/data/media/anime"
    assert ns.prefix_labels(prefixed, "[Anime] ")[0]["name"] == "[Anime] HD-1080p"
