import json

import responses
from requests.exceptions import ConnectionError as UpstreamDown

from conftest import ANI_URL, ENG_URL, OFFSET, make_settings
from proxyseerr.app import create_app

ENG_FOLDERS = [{"id": 1, "path": "/data/media/tv"}]
ANI_FOLDERS = [{"id": 1, "path": "/data/media/anime"}]


def add_folders():
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=ENG_FOLDERS)
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=ANI_FOLDERS)


@responses.activate
def test_merged_series_namespaces_only_the_anime_side(client):
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/series", json=[{"id": 5, "tvdbId": 111, "tags": [2]}]
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/series", json=[{"id": 5, "tvdbId": 222, "tags": [2]}]
    )

    body = client.get("/api/v3/series").get_json()
    assert [s["id"] for s in body] == [5, OFFSET + 5]
    assert [s["tvdbId"] for s in body] == [111, 222]
    assert body[1]["tags"] == [OFFSET + 2]


@responses.activate
def test_unreachable_instance_does_not_blank_the_library(client):
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/series", json=[{"id": 5, "tvdbId": 111}]
    )
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", body=UpstreamDown("down"))

    body = client.get("/api/v3/series").get_json()
    assert [s["id"] for s in body] == [5]


@responses.activate
def test_add_anime_series_routes_and_rewrites_ids(client):
    add_folders()
    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/tag", json=[{"id": 4, "label": "seerr"}])
    responses.add(
        responses.POST,
        f"{ANI_URL}/api/v3/series",
        json={"id": 17, "title": "One Piece", "qualityProfileId": 6, "tags": [4]},
        status=201,
    )

    payload = {
        "title": "One Piece",
        "tvdbId": 81797,
        "rootFolderPath": "/data/media/anime",
        "qualityProfileId": OFFSET + 6,
        "languageProfileId": OFFSET + 2,
        "tags": [OFFSET + 4],
    }
    response = client.post("/api/v3/series", json=payload)
    assert response.status_code == 201

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["qualityProfileId"] == 6
    assert sent["languageProfileId"] == 2
    assert sent["tags"] == [4]
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ANIKEY"

    body = response.get_json()
    assert body["id"] == OFFSET + 17
    assert body["qualityProfileId"] == OFFSET + 6
    assert body["tags"] == [OFFSET + 4]


@responses.activate
def test_add_standard_series_goes_to_english_untouched(client):
    add_folders()
    responses.add(
        responses.POST, f"{ENG_URL}/api/v3/series", json={"id": 17, "title": "Severance"}
    )

    response = client.post(
        "/api/v3/series",
        json={"title": "Severance", "rootFolderPath": "/data/media/tv", "qualityProfileId": 6},
    )
    assert response.get_json()["id"] == 17
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ENGKEY"


@responses.activate
def test_get_series_by_namespaced_id_hits_anime_with_real_id(client):
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/series/17", json={"id": 17, "title": "One Piece"}
    )

    body = client.get(f"/api/v3/series/{OFFSET + 17}").get_json()
    assert body["id"] == OFFSET + 17
    assert responses.calls[-1].request.url.endswith("/api/v3/series/17")


@responses.activate
def test_delete_series_by_namespaced_id(client):
    responses.add(responses.DELETE, f"{ANI_URL}/api/v3/series/17", json={})
    assert client.delete(f"/api/v3/series/{OFFSET + 17}").status_code == 200
    assert responses.calls[-1].request.url.startswith(f"{ANI_URL}/api/v3/series/17")


@responses.activate
def test_lookup_prefers_the_instance_where_the_title_is_added(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/series/lookup",
        json=[{"id": 0, "tvdbId": 81797, "title": "One Piece"}],
    )
    responses.add(
        responses.GET,
        f"{ANI_URL}/api/v3/series/lookup",
        json=[{"id": 17, "tvdbId": 81797, "title": "One Piece"}],
    )

    body = client.get("/api/v3/series/lookup?term=tvdb:81797").get_json()
    assert len(body) == 1
    assert body[0]["id"] == OFFSET + 17


@responses.activate
def test_lookup_keeps_unadded_english_result(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/series/lookup",
        json=[{"id": 0, "tvdbId": 111, "title": "Severance"}],
    )
    responses.add(
        responses.GET,
        f"{ANI_URL}/api/v3/series/lookup",
        json=[{"id": 0, "tvdbId": 111, "title": "Severance"}],
    )

    body = client.get("/api/v3/series/lookup?term=Severance").get_json()
    assert len(body) == 1
    assert body[0]["id"] == 0


@responses.activate
def test_quality_profiles_merge_with_anime_prefix(client):
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/qualityprofile", json=[{"id": 6, "name": "HD-1080p"}]
    )
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/qualityprofile", json=[{"id": 6, "name": "Anime Dual"}]
    )

    body = client.get("/api/v3/qualityprofile").get_json()
    assert body[0] == {"id": 6, "name": "HD-1080p"}
    assert body[1] == {"id": OFFSET + 6, "name": "[Anime] Anime Dual"}


@responses.activate
def test_root_folders_merge_without_touching_paths(client):
    add_folders()
    body = client.get("/api/v3/rootfolder").get_json()
    assert body == [
        {"id": 1, "path": "/data/media/tv"},
        {"id": OFFSET + 1, "path": "/data/media/anime"},
    ]


@responses.activate
def test_command_routes_by_series_id(client):
    responses.add(responses.POST, f"{ANI_URL}/api/v3/command", json={"id": 3, "seriesId": 17})

    body = client.post(
        "/api/v3/command", json={"name": "SeriesSearch", "seriesId": OFFSET + 17}
    ).get_json()

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["seriesId"] == 17
    assert body["id"] == OFFSET + 3
    assert body["seriesId"] == OFFSET + 17


@responses.activate
def test_command_without_ids_is_broadcast(client):
    responses.add(responses.POST, f"{ENG_URL}/api/v3/command", json={"id": 1})
    responses.add(responses.POST, f"{ANI_URL}/api/v3/command", json={"id": 2})

    body = client.post("/api/v3/command", json={"name": "RefreshMonitoredDownloads"}).get_json()
    assert body["id"] == 1
    assert len(responses.calls) == 2


@responses.activate
def test_queue_merges_paged_records(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/queue",
        json={"page": 1, "totalRecords": 1, "records": [{"id": 1, "seriesId": 5}]},
    )
    responses.add(
        responses.GET,
        f"{ANI_URL}/api/v3/queue",
        json={"page": 1, "totalRecords": 1, "records": [{"id": 1, "seriesId": 5}]},
    )

    body = client.get("/api/v3/queue").get_json()
    assert body["totalRecords"] == 2
    assert [r["id"] for r in body["records"]] == [1, OFFSET + 1]
    assert [r["seriesId"] for r in body["records"]] == [5, OFFSET + 5]


@responses.activate
def test_catch_all_routes_by_namespaced_query_param(client):
    responses.add(
        responses.GET,
        f"{ANI_URL}/api/v3/episode",
        json=[{"id": 900, "seriesId": 17, "episodeFileId": 12}],
    )

    body = client.get(f"/api/v3/episode?seriesId={OFFSET + 17}").get_json()
    assert "seriesId=17" in responses.calls[-1].request.url
    assert body[0]["id"] == OFFSET + 900
    assert body[0]["episodeFileId"] == OFFSET + 12


@responses.activate
def test_catch_all_defaults_to_english(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    assert client.get("/api/v3/system/status").get_json()["version"] == "4.0.0"
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ENGKEY"


@responses.activate
def test_upstream_failure_returns_502(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", body=UpstreamDown("nope"))
    response = client.get("/api/v3/system/status")
    assert response.status_code == 502
    assert response.get_json()["error"] == "Upstream connection failed"


@responses.activate
def test_response_drops_hop_by_hop_headers(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/system/status",
        json={"version": "4.0.0"},
        headers={"Transfer-Encoding": "chunked", "Connection": "keep-alive"},
    )
    response = client.get("/api/v3/system/status")
    assert "Transfer-Encoding" not in response.headers
    assert "Connection" not in response.headers


@responses.activate
def test_incoming_api_key_is_replaced_not_forwarded(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    client.get("/api/v3/system/status", headers={"X-Api-Key": "SEERRKEY"})
    assert responses.calls[-1].request.headers["X-Api-Key"] == "ENGKEY"


@responses.activate
def test_inline_apikey_param_is_swapped(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    client.get("/api/v3/system/status?apikey=SEERRKEY")
    assert "apikey=ENGKEY" in responses.calls[-1].request.url


def test_proxy_api_key_is_enforced_when_set():
    app = create_app(make_settings(proxy_api_key="LETMEIN"))
    client = app.test_client()
    assert client.get("/api/v3/series").status_code == 401
    assert client.get("/proxy/health").status_code in (200, 503)


@responses.activate
def test_health_reports_both_instances(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    responses.add(responses.GET, f"{ANI_URL}/api/v3/system/status", body=UpstreamDown("down"))

    response = client.get("/proxy/health")
    body = response.get_json()
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["instances"]["english"]["reachable"] is True
    assert body["instances"]["anime"]["reachable"] is False
