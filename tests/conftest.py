import pytest

from proxyseerr.app import create_app
from proxyseerr.config import Instance, Settings

OFFSET = 1_000_000
ENG_URL = "http://eng:8989"
ANI_URL = "http://ani:8987"


def make_settings(**overrides) -> Settings:
    base = dict(
        english=Instance(key="english", label="ENGLISH", url=ENG_URL, api_key="ENGKEY"),
        anime=Instance(key="anime", label="ANIME", url=ANI_URL, api_key="ANIKEY"),
        id_offset=OFFSET,
        anime_path_match="anime",
        anime_label_prefix="[Anime] ",
        timeout=5,
        port=5000,
        proxy_api_key="",
        log_level="CRITICAL",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def app(settings):
    application = create_app(settings)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def service(app):
    return app.extensions["proxyseerr"]


@pytest.fixture
def client(app):
    return app.test_client()
