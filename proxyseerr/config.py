"""Environment-driven configuration for the proxy."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .kinds import KINDS, MediaKind

# Anime IDs are exposed to the requester shifted by this offset so that a single
# ID space can address both instances. Sonarr/Radarr store IDs as int32, so the
# default leaves ~1.1B of headroom before an offset ID could overflow upstream.
DEFAULT_ID_OFFSET = 1_000_000_000
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

ENGLISH = "english"
ANIME = "anime"


class ConfigError(RuntimeError):
    """Raised for a configuration that would be unsafe or useless to run."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class Instance:
    """One upstream Sonarr/Radarr backend."""

    kind: MediaKind
    key: str
    url: str
    api_key: str

    @property
    def label(self) -> str:
        return f"{self.key.upper()} {self.kind.name.capitalize()}"

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key)

    def endpoint(self, path: str) -> str:
        return f"{self.url}/{path.lstrip('/')}"


@dataclass(frozen=True)
class Service:
    """A Sonarr or Radarr pair served on its own port."""

    kind: MediaKind
    english: Instance
    anime: Instance
    port: int

    @property
    def instances(self) -> tuple[Instance, Instance]:
        return (self.english, self.anime)

    def instance_for(self, key: str) -> Instance:
        return self.anime if key == ANIME else self.english


@dataclass(frozen=True)
class Settings:
    services: tuple[Service, ...]
    id_offset: int
    anime_path_match: str
    anime_label_prefix: str
    timeout: float
    proxy_api_key: str
    allow_anonymous: bool
    max_body_bytes: int
    log_level: str

    def service(self, kind_name: str) -> Service | None:
        for service in self.services:
            if service.kind.name == kind_name:
                return service
        return None


def _validate_url(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(
            f"{name} must be an http(s) URL including host and port, got {url!r}"
        )
    if parts.query or parts.fragment:
        raise ConfigError(f"{name} must not contain a query string or fragment")
    return url.rstrip("/")


def _load_service(kind: MediaKind) -> Service | None:
    prefix = kind.name.upper()
    english_url = _env(f"ENGLISH_{prefix}_URL")
    anime_url = _env(f"ANIME_{prefix}_URL")
    english_key = _env(f"ENGLISH_{prefix}_API_KEY")
    anime_key = _env(f"ANIME_{prefix}_API_KEY")

    if not any([english_url, anime_url, english_key, anime_key]):
        return None

    missing = [
        name
        for name, value in (
            (f"ENGLISH_{prefix}_URL", english_url),
            (f"ENGLISH_{prefix}_API_KEY", english_key),
            (f"ANIME_{prefix}_URL", anime_url),
            (f"ANIME_{prefix}_API_KEY", anime_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"{kind.name} is partially configured; missing: {', '.join(missing)}"
        )

    port = _int_env(f"{prefix}_PROXY_PORT", 0)
    if not port and kind.name == "sonarr":
        # PROXY_PORT was the only port before Radarr support existed.
        port = _int_env("PROXY_PORT", 0)
    port = port or kind.default_port

    return Service(
        kind=kind,
        english=Instance(
            kind=kind,
            key=ENGLISH,
            url=_validate_url(english_url, f"ENGLISH_{prefix}_URL"),
            api_key=english_key,
        ),
        anime=Instance(
            kind=kind,
            key=ANIME,
            url=_validate_url(anime_url, f"ANIME_{prefix}_URL"),
            api_key=anime_key,
        ),
        port=port,
    )


def load_settings() -> Settings:
    services = tuple(
        service for service in (_load_service(kind) for kind in KINDS.values()) if service
    )
    if not services:
        raise ConfigError(
            "No service configured. Set ENGLISH_SONARR_URL/API_KEY and "
            "ANIME_SONARR_URL/API_KEY (and/or the RADARR equivalents)."
        )

    ports = [service.port for service in services]
    if len(set(ports)) != len(ports):
        raise ConfigError(
            "SONARR_PROXY_PORT and RADARR_PROXY_PORT must differ; both are "
            f"currently {ports[0]}"
        )

    proxy_api_key = _env("PROXY_API_KEY")
    allow_anonymous = _bool_env("PROXY_ALLOW_ANONYMOUS")
    if not proxy_api_key and not allow_anonymous:
        raise ConfigError(
            "PROXY_API_KEY is not set. The proxy holds admin API keys for your "
            "servers, so it refuses to run unauthenticated by default. Generate one "
            "with `openssl rand -hex 24` and enter the same value as the API key in "
            "Seerr. To deliberately accept any caller on a trusted network, set "
            "PROXY_ALLOW_ANONYMOUS=true."
        )
    if proxy_api_key and len(proxy_api_key) < 16:
        raise ConfigError("PROXY_API_KEY must be at least 16 characters.")

    return Settings(
        services=services,
        id_offset=_int_env("ANIME_ID_OFFSET", DEFAULT_ID_OFFSET),
        anime_path_match=_env("ANIME_ROOT_FOLDER_MATCH", "anime").lower(),
        anime_label_prefix=_env("ANIME_LABEL_PREFIX", "[Anime] "),
        timeout=float(_int_env("UPSTREAM_TIMEOUT", 20)),
        proxy_api_key=proxy_api_key,
        allow_anonymous=allow_anonymous,
        max_body_bytes=_int_env("MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, minimum=1024),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
