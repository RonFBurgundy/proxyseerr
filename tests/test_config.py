import pytest

from proxyseerr.config import ConfigError, load_settings

ENV_VARS = [
    "ENGLISH_SONARR_URL", "ENGLISH_SONARR_API_KEY", "ANIME_SONARR_URL", "ANIME_SONARR_API_KEY",
    "ENGLISH_RADARR_URL", "ENGLISH_RADARR_API_KEY", "ANIME_RADARR_URL", "ANIME_RADARR_API_KEY",
    "PROXY_PORT", "SONARR_PROXY_PORT", "RADARR_PROXY_PORT",
    "PROXY_API_KEY", "PROXY_ALLOW_ANONYMOUS",
]
KEY = "0123456789abcdef0123"


@pytest.fixture
def env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def set_sonarr(env):
    env.setenv("ENGLISH_SONARR_URL", "http://eng:8989")
    env.setenv("ENGLISH_SONARR_API_KEY", "a")
    env.setenv("ANIME_SONARR_URL", "http://ani:8987")
    env.setenv("ANIME_SONARR_API_KEY", "b")


def set_radarr(env):
    env.setenv("ENGLISH_RADARR_URL", "http://engm:7878")
    env.setenv("ENGLISH_RADARR_API_KEY", "c")
    env.setenv("ANIME_RADARR_URL", "http://anim:7879")
    env.setenv("ANIME_RADARR_API_KEY", "d")


def test_refuses_to_run_unauthenticated_by_default(env):
    set_sonarr(env)
    with pytest.raises(ConfigError, match="PROXY_API_KEY"):
        load_settings()


def test_anonymous_must_be_opted_into_explicitly(env):
    set_sonarr(env)
    env.setenv("PROXY_ALLOW_ANONYMOUS", "true")
    settings = load_settings()
    assert settings.allow_anonymous is True
    assert settings.proxy_api_key == ""


def test_short_keys_are_rejected(env):
    set_sonarr(env)
    env.setenv("PROXY_API_KEY", "short")
    with pytest.raises(ConfigError, match="at least 16"):
        load_settings()


def test_no_service_configured(env):
    env.setenv("PROXY_API_KEY", KEY)
    with pytest.raises(ConfigError, match="No service configured"):
        load_settings()


def test_partial_service_is_an_error(env):
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("ENGLISH_SONARR_URL", "http://eng:8989")
    with pytest.raises(ConfigError, match="partially configured"):
        load_settings()


def test_non_http_urls_are_rejected(env):
    set_sonarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("ANIME_SONARR_URL", "file:///etc/passwd")
    with pytest.raises(ConfigError, match="http"):
        load_settings()


def test_url_with_query_string_is_rejected(env):
    set_sonarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("ANIME_SONARR_URL", "http://ani:8987/?apikey=leak")
    with pytest.raises(ConfigError, match="query string"):
        load_settings()


def test_both_services_get_distinct_default_ports(env):
    set_sonarr(env)
    set_radarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    settings = load_settings()
    ports = {s.kind.name: s.port for s in settings.services}
    assert ports == {"sonarr": 5000, "radarr": 5001}


def test_legacy_proxy_port_still_sets_the_sonarr_port(env):
    set_sonarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("PROXY_PORT", "9000")
    assert load_settings().service("sonarr").port == 9000


def test_colliding_ports_are_rejected(env):
    set_sonarr(env)
    set_radarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("SONARR_PROXY_PORT", "5000")
    env.setenv("RADARR_PROXY_PORT", "5000")
    with pytest.raises(ConfigError, match="must differ"):
        load_settings()


def test_trailing_slashes_are_normalised(env):
    set_sonarr(env)
    env.setenv("PROXY_API_KEY", KEY)
    env.setenv("ENGLISH_SONARR_URL", "http://eng:8989/")
    assert load_settings().service("sonarr").english.url == "http://eng:8989"
