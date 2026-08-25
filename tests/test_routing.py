import pytest
import responses

from conftest import ANI_URL, ENG_URL, OFFSET

ENG_FOLDERS = [{"id": 1, "path": "/data/media/tv"}]
ANI_FOLDERS = [{"id": 1, "path": "/data/media/anime"}]


@pytest.fixture
def folders():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=ENG_FOLDERS)
        mock.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=ANI_FOLDERS)
        yield mock


def test_namespaced_quality_profile_wins(service, folders):
    key, reason = service.router.choose_for_add(
        {"title": "Bleach", "qualityProfileId": OFFSET + 4, "rootFolderPath": "/data/media/tv"}
    )
    assert key == "anime"
    assert "qualityProfileId" in reason


def test_root_folder_exclusive_to_anime(service, folders):
    key, reason = service.router.choose_for_add(
        {"title": "Bleach", "rootFolderPath": "/data/media/anime", "qualityProfileId": 4}
    )
    assert key == "anime"
    assert "only on the anime instance" in reason


def test_root_folder_exclusive_to_english(service, folders):
    key, _ = service.router.choose_for_add(
        {"title": "Severance", "rootFolderPath": "/data/media/tv", "qualityProfileId": 4}
    )
    assert key == "english"


def test_series_type_anime_routes_to_anime(service, folders):
    key, reason = service.router.choose_for_add({"title": "Naruto", "seriesType": "anime"})
    assert key == "anime"
    assert reason == "seriesType is anime"


def test_unknown_path_falls_back_to_keyword_match(service, folders):
    key, reason = service.router.choose_for_add(
        {"title": "Naruto", "rootFolderPath": "/mnt/user/anime-archive"}
    )
    assert key == "anime"
    assert "matches 'anime'" in reason


def test_default_is_english(service, folders):
    key, reason = service.router.choose_for_add({"title": "Severance"})
    assert key == "english"
    assert reason == "default instance"


def test_trailing_slash_and_case_do_not_break_matching(service, folders):
    key, _ = service.router.choose_for_add({"rootFolderPath": "/Data/Media/Anime/"})
    assert key == "anime"


@responses.activate
def test_translate_tags_recreates_foreign_label_on_target(app, service, settings):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[{"id": 3, "label": "seerr-user"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/tag", json=[{"id": 9, "label": "other"}])
    responses.add(responses.POST, f"{ANI_URL}/api/v3/tag", json={"id": 11, "label": "seerr-user"})

    with app.test_request_context("/api/v3/series", method="POST"):
        resolved = service.router.translate_tags([3], settings.anime, {})
    assert resolved == [11]


@responses.activate
def test_translate_tags_reuses_existing_label(app, service, settings):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[{"id": 3, "label": "seerr-user"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/tag", json=[{"id": 9, "label": "seerr-user"}])

    with app.test_request_context("/api/v3/series", method="POST"):
        resolved = service.router.translate_tags([3, OFFSET + 9], settings.anime, {})
    assert resolved == [9, 9]
