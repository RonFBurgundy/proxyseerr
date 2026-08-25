"""Regressions for flaws found in a review sweep of the merge and forward paths."""
import responses
from requests.cookies import create_cookie
from requests.exceptions import ConnectionError as Down

from conftest import (
    ANI_MOVIE_URL,
    ANI_URL,
    ENG_MOVIE_URL,
    ENG_URL,
    OFFSET,
    SONARR_SERVICE,
    build_client,
    make_settings,
)


def test_session_never_replays_a_cookie_to_the_other_instance(service):
    """Two instances usually share a host and differ only by port, and a
    cookie's domain ignores the port."""
    import requests

    session = service.upstream.session
    session.cookies.set_cookie(create_cookie(name="sess", value="SECRET", domain="192.168.0.9"))

    assert dict(session.cookies) == {}
    for port in (8989, 8987):
        prepared = session.prepare_request(
            requests.Request("GET", f"http://192.168.0.9:{port}/api/v3/series")
        )
        assert prepared.headers.get("Cookie") is None


@responses.activate
def test_upstream_cookie_is_not_stored(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/system/status",
        json={"version": "4"},
        headers={"Set-Cookie": "sonarr=abc; Path=/"},
    )
    client.get("/api/v3/system/status")
    service = client.application.extensions["proxyseerr"]
    assert dict(service.upstream.session.cookies) == {}


@responses.activate
def test_lookup_falling_back_to_anime_still_namespaces_it(radarr_client):
    """If English cannot answer, the anime copy is returned - namespaced."""
    responses.add(responses.GET, f"{ENG_MOVIE_URL}/api/v3/movie/lookup/tmdb", body=Down("down"))
    responses.add(
        responses.GET,
        f"{ANI_MOVIE_URL}/api/v3/movie/lookup/tmdb",
        json={"id": 0, "tmdbId": 149, "qualityProfileId": 3, "title": "Akira"},
    )

    body = radarr_client.get("/api/v3/movie/lookup/tmdb?tmdbId=149").get_json()
    assert body["qualityProfileId"] == OFFSET + 3
    assert body["tmdbId"] == 149


@responses.activate
def test_items_without_an_external_id_are_not_reported_as_duplicates(client, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="proxyseerr")
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[{"id": 1, "title": "no tvdb"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", json=[{"id": 1, "title": "also none"}])

    body = client.get("/api/v3/series").get_json()
    assert [s["id"] for s in body] == [1, OFFSET + 1]
    assert "exists on both" not in caplog.text


@responses.activate
def test_real_duplicates_are_still_reported(client, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="proxyseerr")
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[{"id": 1, "tvdbId": 99}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", json=[{"id": 1, "tvdbId": 99}])
    client.get("/api/v3/series")
    assert "exists on both" in caplog.text


@responses.activate
def test_new_tag_is_visible_immediately_after_creation(client):
    """The cache must be dropped after the create, not before."""
    service = client.application.extensions["proxyseerr"]

    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/tag", json=[])
    # Warm the router's tag cache the way translating a tag on an add does.
    service.router._tags(service.service.english)
    assert "tags:english" in service.router._cache

    responses.add(responses.POST, f"{ENG_URL}/api/v3/tag", json={"id": 7, "label": "fresh"})
    assert client.post("/api/v3/tag", json={"label": "fresh"}).status_code == 200
    # Cache dropped, so the next read sees the new tag rather than the stale list.
    assert not service.router._cache


@responses.activate
def test_failed_tag_creation_leaves_the_cache_alone(client):
    service = client.application.extensions["proxyseerr"]
    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[{"id": 1, "label": "a"}])
    service.router._tags(service.service.english)
    warmed = dict(service.router._cache)

    responses.add(responses.POST, f"{ENG_URL}/api/v3/tag", status=400, json={})
    client.post("/api/v3/tag", json={"label": "bad"})
    assert service.router._cache.keys() == warmed.keys()


@responses.activate
def test_api_key_query_param_is_case_insensitive():
    """Sonarr binds query parameters case-insensitively; a client may send apiKey."""
    key = "0123456789abcdef0123"
    _, client = build_client(
        make_settings(proxy_api_key=key, allow_anonymous=False), SONARR_SERVICE
    )
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4"})

    assert client.get(f"/api/v3/system/status?apikey={key}").status_code == 200
    assert client.get(f"/api/v3/system/status?apiKey={key}").status_code == 200
    assert client.get(f"/api/v3/system/status?APIKEY={key}").status_code == 200
    assert client.get("/api/v3/system/status?apiKey=wrong").status_code == 401
