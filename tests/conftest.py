import pytest

from proxyseerr.app import create_app
from proxyseerr.config import DEFAULT_MAX_BODY_BYTES, Instance, Service, Settings
from proxyseerr.kinds import RADARR, SONARR

OFFSET = 1_000_000
ENG_URL = "http://eng:8989"
ANI_URL = "http://ani:8987"
ENG_MOVIE_URL = "http://engmovies:7878"
ANI_MOVIE_URL = "http://animovies:7879"


def make_service(kind, english_url, anime_url, english_key, anime_key, port) -> Service:
    return Service(
        kind=kind,
        english=Instance(kind=kind, key="english", url=english_url, api_key=english_key),
        anime=Instance(kind=kind, key="anime", url=anime_url, api_key=anime_key),
        port=port,
    )


SONARR_SERVICE = make_service(SONARR, ENG_URL, ANI_URL, "ENGKEY", "ANIKEY", 5000)
RADARR_SERVICE = make_service(
    RADARR, ENG_MOVIE_URL, ANI_MOVIE_URL, "ENGMKEY", "ANIMKEY", 5001
)


def make_settings(**overrides) -> Settings:
    base = dict(
        services=(SONARR_SERVICE, RADARR_SERVICE),
        id_offset=OFFSET,
        anime_path_match="anime",
        anime_label_prefix="[Anime] ",
        timeout=5,
        connect_timeout=5,
        proxy_api_key="",
        allow_anonymous=True,
        max_body_bytes=DEFAULT_MAX_BODY_BYTES,
        log_level="CRITICAL",
        request_log="errors",
    )
    base.update(overrides)
    return Settings(**base)


def build_client(settings, service_config):
    app = create_app(settings, service_config)
    app.config.update(TESTING=True)
    return app, app.test_client()


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def sonarr_config():
    return SONARR_SERVICE


@pytest.fixture
def radarr_config():
    return RADARR_SERVICE


@pytest.fixture
def app(settings):
    application, _ = build_client(settings, SONARR_SERVICE)
    return application


@pytest.fixture
def service(app):
    return app.extensions["proxyseerr"]


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def radarr_app(settings):
    application, _ = build_client(settings, RADARR_SERVICE)
    return application


@pytest.fixture
def radarr_client(radarr_app):
    return radarr_app.test_client()
