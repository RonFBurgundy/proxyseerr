"""Environment-driven configuration for the proxy."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Anime IDs are exposed to Seerr shifted by this offset so that a single ID
# space can address both Sonarr instances. Sonarr stores IDs as int32, so the
# default leaves ~1.1B of headroom before an offset ID could overflow upstream.
DEFAULT_ID_OFFSET = 1_000_000_000


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


@dataclass(frozen=True)
class Instance:
    """One upstream Sonarr backend."""

    key: str
    label: str
    url: str
    api_key: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key)

    def endpoint(self, path: str) -> str:
        return f"{self.url}/{path.lstrip('/')}"


@dataclass(frozen=True)
class Settings:
    english: Instance
    anime: Instance
    id_offset: int
    anime_path_match: str
    anime_label_prefix: str
    timeout: float
    port: int
    proxy_api_key: str
    log_level: str

    @property
    def instances(self) -> tuple[Instance, Instance]:
        return (self.english, self.anime)

    def instance_for(self, key: str) -> Instance:
        return self.anime if key == "anime" else self.english


def load_settings() -> Settings:
    english = Instance(
        key="english",
        label="ENGLISH",
        url=_env("ENGLISH_SONARR_URL", "http://localhost:8989").rstrip("/"),
        api_key=_env("ENGLISH_SONARR_API_KEY"),
    )
    anime = Instance(
        key="anime",
        label="ANIME",
        url=_env("ANIME_SONARR_URL", "http://localhost:8987").rstrip("/"),
        api_key=_env("ANIME_SONARR_API_KEY"),
    )
    try:
        id_offset = int(_env("ANIME_ID_OFFSET", str(DEFAULT_ID_OFFSET)))
    except ValueError:
        id_offset = DEFAULT_ID_OFFSET
    if id_offset <= 0:
        id_offset = DEFAULT_ID_OFFSET

    return Settings(
        english=english,
        anime=anime,
        id_offset=id_offset,
        anime_path_match=_env("ANIME_ROOT_FOLDER_MATCH", "anime").lower(),
        anime_label_prefix=_env("ANIME_LABEL_PREFIX", "[Anime] "),
        timeout=float(_env("UPSTREAM_TIMEOUT", "20")),
        port=int(_env("PROXY_PORT", "5000")),
        proxy_api_key=_env("PROXY_API_KEY"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
