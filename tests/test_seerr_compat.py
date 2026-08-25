"""Regressions against how Seerr actually calls Sonarr and Radarr.

Paths and payload shapes here were taken from seerr-team/seerr's
server/api/servarr/{base,sonarr,radarr}.ts.
"""
import json

import responses

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


@responses.activate
def test_camelcase_quality_profile_still_merges(client):
    """Seerr asks for /qualityProfile; Werkzeug would not match it lowercase."""
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/qualityprofile", json=[{"id": 6, "name": "HD-1080p"}]
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/qualityprofile", json=[{"id": 6, "name": "Dual Audio"}]
    )

    body = client.get("/api/v3/qualityProfile").get_json()
    assert [p["id"] for p in body] == [6, OFFSET + 6]
    assert body[1]["name"] == "[Anime] Dual Audio"


@responses.activate
def test_mixed_case_paths_reach_their_routes(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/tv"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/an"}])
    for spelling in ("/api/v3/rootFolder", "/api/v3/ROOTFOLDER", "/api/v3/rootfolder"):
        body = client.get(spelling).get_json()
        assert [f["id"] for f in body] == [1, OFFSET + 1], spelling


@responses.activate
def test_episode_monitor_routes_on_body_ids(client):
    """PUT /episode/monitor carries IDs only in the body."""
    responses.add(responses.PUT, f"{ANI_URL}/api/v3/episode/monitor", json=[])

    response = client.put(
        "/api/v3/episode/monitor",
        json={"episodeIds": [OFFSET + 41, OFFSET + 42], "monitored": True},
    )
    assert response.status_code == 200
    assert responses.calls[-1].request.url.startswith(f"{ANI_URL}/api/v3/episode/monitor")
    sent = json.loads(responses.calls[-1].request.body)
    assert sent["episodeIds"] == [41, 42]
    assert sent["monitored"] is True


@responses.activate
def test_episode_monitor_for_english_ids_stays_on_english(client):
    responses.add(responses.PUT, f"{ENG_URL}/api/v3/episode/monitor", json=[])
    client.put("/api/v3/episode/monitor", json={"episodeIds": [41], "monitored": True})
    assert responses.calls[-1].request.url.startswith(f"{ENG_URL}/api/v3/episode/monitor")
    assert json.loads(responses.calls[-1].request.body)["episodeIds"] == [41]


@responses.activate
def test_radarr_legacy_profile_id_is_translated(radarr_client):
    """Seerr's addMovie sends both qualityProfileId and the legacy profileId."""
    responses.add(responses.GET, f"{ENG_MOVIE_URL}/api/v3/rootfolder", json=[])
    responses.add(
        responses.GET, f"{ANI_MOVIE_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}]
    )
    responses.add(responses.POST, f"{ANI_MOVIE_URL}/api/v3/movie", json={"id": 5})

    radarr_client.post(
        "/api/v3/movie",
        json={
            "title": "Akira",
            "rootFolderPath": "/anime",
            "qualityProfileId": OFFSET + 3,
            "profileId": OFFSET + 3,
            "minimumAvailability": "released",
        },
    )
    sent = json.loads(responses.calls[-1].request.body)
    assert sent["qualityProfileId"] == 3
    assert sent["profileId"] == 3


@responses.activate
def test_endpoint_missing_on_both_returns_its_status_not_502(client):
    """Sonarr v4 dropped /languageprofile; both 404 is an answer, not an outage."""
    responses.add(responses.GET, f"{ENG_URL}/api/v3/languageprofile", status=404, json={})
    responses.add(responses.GET, f"{ANI_URL}/api/v3/languageprofile", status=404, json={})

    response = client.get("/api/v3/languageprofile")
    assert response.status_code == 404
    assert "404" in response.get_json()["error"]


@responses.activate
def test_disagreeing_failures_are_still_502(client):
    from requests.exceptions import ConnectionError as Down

    responses.add(responses.GET, f"{ENG_URL}/api/v3/languageprofile", status=404, json={})
    responses.add(responses.GET, f"{ANI_URL}/api/v3/languageprofile", body=Down("down"))
    assert client.get("/api/v3/languageprofile").status_code == 502


@responses.activate
def test_series_lookup_by_tvdb_term(client):
    """Seerr checks for an existing series with /series/lookup?term=tvdb:{id}."""
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/series/lookup", json=[{"id": 0, "tvdbId": 81797}]
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/series/lookup", json=[{"id": 17, "tvdbId": 81797}]
    )
    body = client.get("/api/v3/series/lookup?term=tvdb%3A81797").get_json()
    assert body[0]["id"] == OFFSET + 17
    assert "term=tvdb%3A81797" in responses.calls[-1].request.url


@responses.activate
def test_delete_series_passes_through_its_query_flags(client):
    responses.add(responses.DELETE, f"{ANI_URL}/api/v3/series/17", json={})
    client.delete(f"/api/v3/series/{OFFSET + 17}?deleteFiles=true&addImportExclusion=false")
    url = responses.calls[-1].request.url
    assert "deleteFiles=true" in url and "addImportExclusion=false" in url


def test_non_ascii_api_key_does_not_crash():
    _, client = build_client(
        make_settings(proxy_api_key="0123456789abcdef0123", allow_anonymous=False),
        SONARR_SERVICE,
    )
    response = client.get("/api/v3/series", headers={"X-Api-Key": "ünïcödé-key-value"})
    assert response.status_code == 401


@responses.activate
def test_trailing_slash_does_not_redirect(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[])
    assert client.get("/api/v3/rootfolder/").status_code == 200


@responses.activate
def test_add_without_language_profile_is_untouched(client):
    """Sonarr v4 dropped language profiles; the key is simply absent."""
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[])
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}]
    )
    responses.add(responses.POST, f"{ANI_URL}/api/v3/series", json={"id": 9, "title": "Naruto"})

    payload = {
        "title": "Naruto",
        "tvdbId": 78857,
        "rootFolderPath": "/anime",
        "qualityProfileId": OFFSET + 6,
        "monitorNewItems": "all",
        "seasons": [{"seasonNumber": 1, "monitored": True}],
        "addOptions": {"ignoreEpisodesWithFiles": False, "searchForMissingEpisodes": True},
    }
    response = client.post("/api/v3/series", json=payload)
    assert response.status_code == 200

    sent = json.loads(responses.calls[-1].request.body)
    assert "languageProfileId" not in sent
    assert sent["qualityProfileId"] == 6
    # Everything Seerr sent that is not an ID must survive verbatim.
    assert sent["seasons"] == payload["seasons"]
    assert sent["addOptions"] == payload["addOptions"]
    assert sent["monitorNewItems"] == "all"


@responses.activate
def test_language_profile_id_is_translated_when_present(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[])
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/anime"}]
    )
    responses.add(responses.POST, f"{ANI_URL}/api/v3/series", json={"id": 9})

    client.post(
        "/api/v3/series",
        json={"title": "Naruto", "rootFolderPath": "/anime", "languageProfileId": OFFSET + 2},
    )
    assert json.loads(responses.calls[-1].request.body)["languageProfileId"] == 2
