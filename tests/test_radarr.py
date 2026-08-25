import json

import responses

from conftest import ANI_MOVIE_URL, ENG_MOVIE_URL, OFFSET

ENG_FOLDERS = [{"id": 1, "path": "/data/media/movies"}]
ANI_FOLDERS = [{"id": 1, "path": "/data/media/anime-movies"}]


def add_folders():
    responses.add(responses.GET, f"{ENG_MOVIE_URL}/api/v3/rootfolder", json=ENG_FOLDERS)
    responses.add(responses.GET, f"{ANI_MOVIE_URL}/api/v3/rootfolder", json=ANI_FOLDERS)


@responses.activate
def test_merged_movies_namespace_the_anime_side(radarr_client):
    responses.add(
        responses.GET, f"{ENG_MOVIE_URL}/api/v3/movie", json=[{"id": 5, "tmdbId": 111, "tags": [2]}]
    )
    responses.add(
        responses.GET, f"{ANI_MOVIE_URL}/api/v3/movie", json=[{"id": 5, "tmdbId": 222, "tags": [2]}]
    )

    body = radarr_client.get("/api/v3/movie").get_json()
    assert [m["id"] for m in body] == [5, OFFSET + 5]
    assert [m["tmdbId"] for m in body] == [111, 222]
    assert body[1]["tags"] == [OFFSET + 2]


@responses.activate
def test_add_anime_movie_routes_and_rewrites_ids(radarr_client):
    add_folders()
    responses.add(responses.GET, f"{ENG_MOVIE_URL}/api/v3/tag", json=[])
    responses.add(responses.GET, f"{ANI_MOVIE_URL}/api/v3/tag", json=[{"id": 4, "label": "seerr"}])
    responses.add(
        responses.POST,
        f"{ANI_MOVIE_URL}/api/v3/movie",
        json={"id": 22, "title": "Akira", "qualityProfileId": 3, "tags": [4]},
        status=201,
    )

    response = radarr_client.post(
        "/api/v3/movie",
        json={
            "title": "Akira",
            "tmdbId": 149,
            "rootFolderPath": "/data/media/anime-movies",
            "qualityProfileId": OFFSET + 3,
            "minimumAvailability": "released",
            "tags": [OFFSET + 4],
            "addOptions": {"searchForMovie": True},
        },
    )
    assert response.status_code == 201

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["qualityProfileId"] == 3
    assert sent["tags"] == [4]
    assert sent["minimumAvailability"] == "released"
    assert sent["addOptions"] == {"searchForMovie": True}
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ANIMKEY"
    assert response.get_json()["id"] == OFFSET + 22


@responses.activate
def test_add_standard_movie_goes_to_english(radarr_client):
    add_folders()
    responses.add(responses.POST, f"{ENG_MOVIE_URL}/api/v3/movie", json={"id": 22})

    body = radarr_client.post(
        "/api/v3/movie", json={"title": "Dune", "rootFolderPath": "/data/media/movies"}
    ).get_json()
    assert body["id"] == 22
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ENGMKEY"


@responses.activate
def test_movie_by_namespaced_id(radarr_client):
    responses.add(responses.GET, f"{ANI_MOVIE_URL}/api/v3/movie/22", json={"id": 22})
    body = radarr_client.get(f"/api/v3/movie/{OFFSET + 22}").get_json()
    assert body["id"] == OFFSET + 22
    assert responses.calls[-1].request.url.endswith("/api/v3/movie/22")


@responses.activate
def test_tmdb_lookup_prefers_the_instance_holding_the_movie(radarr_client):
    responses.add(
        responses.GET, f"{ENG_MOVIE_URL}/api/v3/movie/lookup/tmdb", json={"id": 0, "tmdbId": 149}
    )
    responses.add(
        responses.GET, f"{ANI_MOVIE_URL}/api/v3/movie/lookup/tmdb", json={"id": 22, "tmdbId": 149}
    )

    body = radarr_client.get("/api/v3/movie/lookup/tmdb?tmdbId=149").get_json()
    assert body["id"] == OFFSET + 22


@responses.activate
def test_movie_command_routes_by_movie_id(radarr_client):
    responses.add(responses.POST, f"{ANI_MOVIE_URL}/api/v3/command", json={"id": 3, "movieId": 22})

    body = radarr_client.post(
        "/api/v3/command", json={"name": "MoviesSearch", "movieIds": [OFFSET + 22]}
    ).get_json()

    assert json.loads(responses.calls[-1].request.body)["movieIds"] == [22]
    assert body["id"] == OFFSET + 3
    assert body["movieId"] == OFFSET + 22


@responses.activate
def test_movie_queue_merges_and_namespaces(radarr_client):
    responses.add(
        responses.GET,
        f"{ENG_MOVIE_URL}/api/v3/queue",
        json={"totalRecords": 1, "records": [{"id": 1, "movieId": 5}]},
    )
    responses.add(
        responses.GET,
        f"{ANI_MOVIE_URL}/api/v3/queue",
        json={"totalRecords": 1, "records": [{"id": 1, "movieId": 5}]},
    )

    body = radarr_client.get("/api/v3/queue").get_json()
    assert body["totalRecords"] == 2
    assert [r["movieId"] for r in body["records"]] == [5, OFFSET + 5]


@responses.activate
def test_catch_all_routes_by_movie_id_param(radarr_client):
    responses.add(
        responses.GET, f"{ANI_MOVIE_URL}/api/v3/moviefile", json=[{"id": 8, "movieId": 22}]
    )
    body = radarr_client.get(f"/api/v3/moviefile?movieId={OFFSET + 22}").get_json()
    assert "movieId=22" in responses.calls[-1].request.url
    assert body[0]["id"] == OFFSET + 8


def test_radarr_has_no_language_profile_route(radarr_app, app):
    sonarr_rules = {r.rule for r in app.url_map.iter_rules()}
    radarr_rules = {r.rule for r in radarr_app.url_map.iter_rules()}
    assert "/api/v3/languageprofile" in sonarr_rules
    assert "/api/v3/languageprofile" not in radarr_rules
    assert "/api/v3/movie" in radarr_rules
    assert "/api/v3/series" in sonarr_rules
