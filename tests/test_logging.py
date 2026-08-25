"""Nothing may fail silently: every degraded path must leave a log line."""
import logging

import pytest
import responses
from requests.exceptions import ConnectionError as Down

from conftest import ANI_URL, ENG_URL, OFFSET, SONARR_SERVICE, build_client, make_settings


@pytest.fixture
def loud(caplog):
    caplog.set_level(logging.DEBUG, logger="proxyseerr")
    return caplog


@responses.activate
def test_upstream_rejection_is_logged_with_the_reason(client, loud):
    responses.add(
        responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/data/tv"}]
    )
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[])
    responses.add(
        responses.POST,
        f"{ENG_URL}/api/v3/series",
        status=400,
        json=[{"errorMessage": "Invalid quality profile"}],
    )

    response = client.post(
        "/api/v3/series", json={"title": "Severance", "rootFolderPath": "/data/tv"}
    )
    assert response.status_code == 400
    assert "Invalid quality profile" in loud.text
    assert "rejected POST /api/v3/series with HTTP 400" in loud.text


@responses.activate
def test_failed_requests_are_logged_by_default(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", status=500, json={})
    client.get("/api/v3/system/status")
    assert "GET /api/v3/system/status -> 500 via ENGLISH Sonarr" in loud.text


@responses.activate
def test_successful_requests_are_quiet_unless_request_log_is_all(loud):
    _, quiet_client = build_client(make_settings(), SONARR_SERVICE)
    _, loud_client = build_client(make_settings(request_log="all"), SONARR_SERVICE)
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4"})

    quiet_client.get("/api/v3/system/status")
    assert "-> 200" not in loud.text

    loud_client.get("/api/v3/system/status")
    assert "GET /api/v3/system/status -> 200 via ENGLISH Sonarr" in loud.text


@responses.activate
def test_request_log_off_silences_even_failures(loud):
    _, client = build_client(make_settings(request_log="off"), SONARR_SERVICE)
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", status=500, json={})
    client.get("/api/v3/system/status")
    assert "-> 500" not in loud.text


@responses.activate
def test_request_log_never_contains_the_api_key(loud):
    _, client = build_client(make_settings(request_log="all"), SONARR_SERVICE)
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4"})
    client.get("/api/v3/system/status?apikey=supersecretvalue")
    assert "supersecretvalue" not in loud.text
    assert "apikey=<redacted>" in loud.text


@responses.activate
def test_unreachable_instance_names_itself_in_the_log(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", body=Down("refused"))
    client.get("/api/v3/series")
    assert "ANIME Sonarr unreachable" in loud.text


@responses.activate
def test_upstream_error_status_on_a_merge_read_is_logged(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[])
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/series", status=401, json={"message": "Unauthorized"}
    )
    client.get("/api/v3/series")
    assert "ANIME Sonarr returned HTTP 401" in loud.text
    assert "Unauthorized" in loud.text


@responses.activate
def test_unexpected_payload_shape_is_logged(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", json={"unexpected": "object"})
    client.get("/api/v3/series")
    assert "expected a list" in loud.text


def test_malformed_json_body_is_logged(client, loud):
    client.post("/api/v3/series", data=b"{not json", content_type="application/json")
    assert "not valid JSON" in loud.text


@responses.activate
def test_dropped_tag_is_logged(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", json=[])
    responses.add(
        responses.GET, f"{ANI_URL}/api/v3/rootfolder", json=[{"id": 1, "path": "/data/anime"}]
    )
    responses.add(responses.GET, f"{ENG_URL}/api/v3/tag", json=[{"id": 3, "label": "wanted"}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/tag", json=[])
    responses.add(responses.POST, f"{ANI_URL}/api/v3/tag", status=500, json={})
    responses.add(responses.POST, f"{ANI_URL}/api/v3/series", json={"id": 1})

    client.post(
        "/api/v3/series",
        json={"title": "Naruto", "rootFolderPath": "/data/anime", "tags": [3]},
    )
    assert "Could not create tag 'wanted'" in loud.text
    assert "dropped from this request" in loud.text


def test_refused_path_is_logged(client, loud):
    client.get("/config.xml")
    assert "Refusing to forward path" in loud.text


def test_unauthenticated_request_is_logged():
    import logging as _logging

    _, client = build_client(
        make_settings(proxy_api_key="0123456789abcdef0123", allow_anonymous=False),
        SONARR_SERVICE,
    )
    logger = _logging.getLogger("proxyseerr.app")
    records = []
    handler = _logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        client.get("/api/v3/series")
    finally:
        logger.removeHandler(handler)
    assert any("Rejected unauthenticated" in r.getMessage() for r in records)


def test_errors_are_json_not_html(client):
    # A path outside the forwarding surface is refused by the proxy itself.
    response = client.get("/config.xml")
    assert response.status_code == 404
    assert response.is_json
    assert "error" in response.get_json()

    # Requests to the proxy's own namespace are not forwarded either.
    response = client.post("/proxy/health")
    assert response.status_code == 404
    assert response.is_json
    assert "<html" not in response.get_data(as_text=True).lower()


def test_unexpected_exception_is_logged_and_returns_json(client, loud, monkeypatch):
    service = client.application.extensions["proxyseerr"]

    def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(service, "merged_items", boom)
    response = client.get("/api/v3/series")
    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal proxy error"}
    assert "Unhandled error serving GET /api/v3/series" in loud.text
    assert "kaboom" in loud.text


@responses.activate
def test_total_outage_is_an_error_not_an_empty_library(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", body=Down("refused"))
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", body=Down("refused"))

    response = client.get("/api/v3/series")
    assert response.status_code == 502
    assert response.get_json() == {"error": "No instance could be reached"}
    assert "Every instance failed" in loud.text


@responses.activate
def test_partial_outage_returns_data_and_says_it_is_incomplete(client, loud):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/series", json=[{"id": 1, "tvdbId": 9}])
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series", body=Down("refused"))

    response = client.get("/api/v3/series")
    assert response.status_code == 200
    assert [s["id"] for s in response.get_json()] == [1]
    assert "the result is incomplete" in loud.text
    assert "ANIME Sonarr" in loud.text


@responses.activate
def test_total_outage_on_root_folders_is_also_an_error(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/rootfolder", body=Down("refused"))
    responses.add(responses.GET, f"{ANI_URL}/api/v3/rootfolder", body=Down("refused"))
    assert client.get("/api/v3/rootfolder").status_code == 502


@responses.activate
def test_slow_upstream_calls_warn_before_they_time_out(client, loud):
    service = client.application.extensions["proxyseerr"]
    service.upstream.slow_after = 0  # any duration counts as slow
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4"})

    client.get("/api/v3/system/status")
    assert "raise UPSTREAM_TIMEOUT" in loud.text
    assert "ENGLISH Sonarr took" in loud.text


def test_connect_timeout_is_separate_and_bounded_by_the_read_timeout():
    from proxyseerr.upstream import Upstream

    assert Upstream(timeout=20).connect_timeout == 5
    assert Upstream(timeout=3).connect_timeout == 3  # never exceeds the read budget
    assert Upstream(timeout=20).slow_after == 10


@responses.activate
def test_periodic_broadcasts_do_not_spam_the_log(client, caplog):
    """Seerr polls RefreshMonitoredDownloads every minute; INFO would drown."""
    import logging as _logging

    responses.add(responses.POST, f"{ENG_URL}/api/v3/command", json={"id": 1})
    responses.add(responses.POST, f"{ANI_URL}/api/v3/command", json={"id": 2})

    caplog.set_level(_logging.INFO, logger="proxyseerr")
    client.post("/api/v3/command", json={"name": "RefreshMonitoredDownloads"})
    assert "Broadcasting" not in caplog.text

    caplog.clear()
    caplog.set_level(_logging.DEBUG, logger="proxyseerr")
    client.post("/api/v3/command", json={"name": "RefreshMonitoredDownloads"})
    assert "Broadcasting command 'RefreshMonitoredDownloads'" in caplog.text


@responses.activate
def test_routed_commands_are_still_logged_at_info(client, caplog):
    import logging as _logging

    responses.add(responses.POST, f"{ANI_URL}/api/v3/command", json={"id": 3})
    caplog.set_level(_logging.INFO, logger="proxyseerr")
    client.post("/api/v3/command", json={"name": "MissingEpisodeSearch", "seriesId": OFFSET + 17})
    assert "Routing command 'MissingEpisodeSearch' to ANIME Sonarr" in caplog.text


@responses.activate
def test_delete_is_always_logged_even_when_only_errors_are(client, loud):
    """The one call that destroys data must never be silent."""
    responses.add(responses.DELETE, f"{ANI_URL}/api/v3/series/17", json={})

    client.delete(f"/api/v3/series/{OFFSET + 17}?deleteFiles=true&addImportExclusion=false")

    assert "Routing DELETE of series 17 to ANIME Sonarr (deleteFiles=true)" in loud.text
    assert "DELETE /api/v3/series/" in loud.text
    assert "-> 200 via ANIME Sonarr" in loud.text


@responses.activate
def test_delete_to_english_names_that_instance(client, loud):
    responses.add(responses.DELETE, f"{ENG_URL}/api/v3/series/17", json={})
    client.delete("/api/v3/series/17?deleteFiles=false")
    assert "Routing DELETE of series 17 to ENGLISH Sonarr (deleteFiles=false)" in loud.text


@responses.activate
def test_movie_delete_is_logged_too(radarr_client, loud):
    from conftest import ANI_MOVIE_URL

    responses.add(responses.DELETE, f"{ANI_MOVIE_URL}/api/v3/movie/22", json={})
    radarr_client.delete(f"/api/v3/movie/{OFFSET + 22}?deleteFiles=true")
    assert "Routing DELETE of movie 22 to ANIME Radarr (deleteFiles=true)" in loud.text


@responses.activate
def test_request_log_off_still_silences_deletes(loud):
    _, client = build_client(make_settings(request_log="off"), SONARR_SERVICE)
    responses.add(responses.DELETE, f"{ENG_URL}/api/v3/series/17", json={})
    client.delete("/api/v3/series/17")
    # The routing line stays - it is not part of the access log - but no
    # access line is added.
    assert "-> 200 via" not in loud.text


@responses.activate
def test_successful_reads_stay_quiet(client, loud):
    responses.add(responses.GET, f"{ANI_URL}/api/v3/series/17", json={"id": 17})
    client.get(f"/api/v3/series/{OFFSET + 17}")
    assert "-> 200" not in loud.text
