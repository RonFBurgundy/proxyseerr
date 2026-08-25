"""One instance down, one up, and everything in between."""
import pytest
import responses
from requests.exceptions import ConnectionError as Down
from responses import matchers

from conftest import ANI_URL, ENG_URL, OFFSET

ENG_KEY = matchers.header_matcher({"X-Api-Key": "ENGKEY"})
ANI_KEY = matchers.header_matcher({"X-Api-Key": "ANIKEY"})


@responses.activate
def test_internal_root_folder_lookup_is_authenticated(service):
    """The mock only answers with the right key, so a missing one fails the test.

    These lookups have no inbound request to copy headers from, and every
    /api/v3 endpoint rejects a request without X-Api-Key.
    """
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/tv"}],
        match=[ENG_KEY],
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}],
        match=[ANI_KEY],
    )

    key, reason = service.router.choose_for_add({"rootFolderPath": "/anime"})
    assert key == "anime"
    assert "only on the anime instance" in reason


@responses.activate
def test_internal_tag_lookup_is_authenticated(app, service):
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/tag", json=[{"id": 3, "label": "shared"}],
        match=[ENG_KEY],
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/tag", json=[{"id": 9, "label": "shared"}],
        match=[ANI_KEY],
    )
    with app.test_request_context("/api/v3/series", method="POST"):
        assert service.router.translate_tags([3], service.service.anime, {}) == [9]


@responses.activate
def test_a_failed_lookup_is_not_cached(service):
    """After a reboot the proxy can be up before Sonarr is."""
    anime = service.service.anime
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", body=Down("not up yet"))
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}]
    )

    assert service.router.root_folder_paths(anime) is None
    # Not stuck on the failure for the whole TTL.
    assert service.router.root_folder_paths(anime) == {"/anime"}


@responses.activate
def test_a_successful_lookup_is_cached(service):
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}]
    )
    anime = service.service.anime
    assert service.router.root_folder_paths(anime) == {"/anime"}
    assert service.router.root_folder_paths(anime) == {"/anime"}
    assert len(responses.calls) == 1


@responses.activate
def test_unreadable_instance_never_hands_its_titles_to_the_other(service, caplog):
    """The hazard: the English instance also lists the anime path.

    Older guides told people to add a dummy anime root folder to the default
    instance. With the anime instance unreachable, "exists only on the English
    instance" would then be true - and an anime request would land on the
    English server and succeed there.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="proxyseerr")
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/rootfolder",
        json=[{"id": 1, "path": "/tv"}, {"id": 2, "path": "/anime"}],
    )
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", body=Down("down"))

    key, reason = service.router.choose_for_add({"rootFolderPath": "/anime"})
    assert key == "anime"
    assert "matches 'anime'" in reason
    assert "root-folder routing is unavailable" in caplog.text


@responses.activate
def test_ownership_routing_resumes_once_the_instance_is_back(service):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", body=Down("down"))
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/tv"}])
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}],
    )

    first, _ = service.router.choose_for_add({"rootFolderPath": "/tv"})
    assert first == "english"  # by default, not by ownership

    second, reason = service.router.choose_for_add({"rootFolderPath": "/tv"})
    assert second == "english"
    assert "only on the english instance" in reason


@responses.activate
def test_adds_still_route_while_one_instance_is_down(client):
    """A down instance must not stop the other one working."""
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/tv"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", body=Down("down"))
    responses.add(responses.POST, f"{ENG_URL}/api/v3/series", json={"id": 4})

    response = client.post(
        "/api/v3/series", json={"title": "Severance", "rootFolderPath": "/tv"}
    )
    assert response.status_code == 200
    assert response.get_json()["id"] == 4


@responses.activate
def test_an_add_to_a_down_instance_fails_loudly(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}])
    responses.add(responses.POST, f"{ANI_URL}/api/v3/series", body=Down("down"))

    response = client.post(
        "/api/v3/series", json={"title": "Naruto", "rootFolderPath": "/anime"}
    )
    assert response.status_code == 502
    assert "ANIME Sonarr" in response.get_json()["error"]


@responses.activate
def test_health_distinguishes_which_instance_is_down(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4"})
    responses.add(responses.GET, f"{ANI_URL}/api/v3/system/status", body=Down("down"))

    response = client.get("/proxy/health")
    body = response.get_json()
    assert response.status_code == 503
    assert body["instances"]["english"]["reachable"] is True
    assert body["instances"]["anime"]["reachable"] is False
