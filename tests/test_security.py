import pytest
import responses

from conftest import ENG_URL, SONARR_SERVICE, build_client, make_settings
from proxyseerr.app import safe_path
from proxyseerr.upstream import redact

KEY = "0123456789abcdef0123"


@pytest.fixture
def secured():
    return build_client(
        make_settings(proxy_api_key=KEY, allow_anonymous=False), SONARR_SERVICE
    )[1]


def test_requests_without_the_key_are_rejected(secured):
    assert secured.get("/api/v3/series").status_code == 401
    assert secured.get("/api/v3/system/status").status_code == 401
    assert secured.post("/api/v3/series", json={"title": "x"}).status_code == 401


def test_wrong_key_is_rejected(secured):
    assert secured.get("/api/v3/series", headers={"X-Api-Key": "wrong"}).status_code == 401
    assert secured.get(f"/api/v3/series?apikey={KEY[:-1]}x").status_code == 401


@responses.activate
def test_correct_key_is_accepted_by_header_or_query(secured):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    assert secured.get("/api/v3/system/status", headers={"X-Api-Key": KEY}).status_code == 200
    assert secured.get(f"/api/v3/system/status?apikey={KEY}").status_code == 200


@responses.activate
def test_health_hides_internals_from_unauthenticated_callers(secured):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    body = secured.get("/proxy/health").get_json()
    assert body["instances"]["english"] == {"reachable": True}
    assert "url" not in body["instances"]["english"]
    assert "idOffset" not in body
    assert ENG_URL not in str(body)


@responses.activate
def test_health_is_detailed_for_authenticated_callers(secured):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    body = secured.get("/proxy/health", headers={"X-Api-Key": KEY}).get_json()
    assert body["instances"]["english"]["url"] == ENG_URL
    assert body["instances"]["english"]["version"] == "4.0.0"


@pytest.mark.parametrize(
    "path",
    ["etc/passwd", "..", "api/../../etc/passwd", "api/v3/..", "config.xml", ".env", "initialize.js"],
)
def test_paths_outside_the_api_are_refused(path):
    assert safe_path(path) is None


@pytest.mark.parametrize("path", ["api/v3/series", "api/v3/queue/details", "ping"])
def test_api_paths_are_allowed(path):
    assert safe_path(path) == "/" + path


def test_control_characters_are_refused():
    assert safe_path("api/v3/series\r\nX-Injected: 1") is None
    assert safe_path("api\\v3\\series") is None


@responses.activate
def test_catch_all_refuses_non_api_paths_without_calling_upstream(client):
    responses.add(responses.GET, f"{ENG_URL}/config.xml", json={"secret": "leaked"})
    response = client.get("/config.xml")
    assert response.status_code == 404
    assert len(responses.calls) == 0


@responses.activate
def test_caller_headers_are_not_relayed_upstream(client):
    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", json={"version": "4.0.0"})
    client.get(
        "/api/v3/system/status",
        headers={
            "Cookie": "session=secret",
            "Authorization": "Bearer secret",
            "X-Forwarded-For": "10.1.2.3",
        },
    )
    sent = responses.calls[-1].request.headers
    assert "Cookie" not in sent
    assert "Authorization" not in sent
    assert "X-Forwarded-For" not in sent
    assert sent["X-Api-Key"] == "ENGKEY"


@responses.activate
def test_upstream_cookies_and_banners_are_not_returned(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/system/status",
        json={"version": "4.0.0"},
        headers={"Set-Cookie": "sonarr=abc; Path=/", "Server": "Kestrel"},
    )
    response = client.get("/api/v3/system/status")
    assert "Set-Cookie" not in response.headers
    assert response.headers.get("Server") != "Kestrel"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_oversized_bodies_are_rejected():
    _, client = build_client(make_settings(max_body_bytes=1024), SONARR_SERVICE)
    response = client.post(
        "/api/v3/series",
        data=b"x" * 4096,
        content_type="application/json",
    )
    assert response.status_code == 413


def test_redact_strips_credentials_from_log_text():
    assert "supersecret" not in redact("GET /api/v3/series?apikey=supersecret&page=1")
    assert "supersecret" not in redact("url: http://h/api?api_key=supersecret")
    assert "supersecret" not in redact("token=supersecret")
    assert "page=1" in redact("GET /api/v3/series?apikey=supersecret&page=1")


@responses.activate
def test_upstream_error_body_does_not_leak_internal_urls(client):
    from requests.exceptions import ConnectionError as Down

    responses.add(responses.GET, f"{ENG_URL}/api/v3/system/status", body=Down("boom"))
    response = client.get("/api/v3/system/status")
    assert response.status_code == 502
    assert ENG_URL not in response.get_data(as_text=True)
    assert "boom" not in response.get_data(as_text=True)


@responses.activate
def test_redirects_are_not_followed(client):
    responses.add(
        responses.GET,
        f"{ENG_URL}/api/v3/system/status",
        status=302,
        headers={"Location": "http://attacker.invalid/"},
    )
    response = client.get("/api/v3/system/status")
    assert response.status_code == 302
    assert "Location" not in response.headers
