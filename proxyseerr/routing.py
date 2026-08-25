"""Deciding which instance of a pair a request belongs to."""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import ANIME, ENGLISH, Instance, Service, Settings
from .namespace import decode_id, is_anime_id, strip_prefix
from .upstream import Upstream

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60


class Router:
    def __init__(self, settings: Settings, service: Service, upstream: Upstream):
        self.settings = settings
        self.service = service
        self.kind = service.kind
        self.upstream = upstream
        self._cache: dict[str, tuple[float, Any]] = {}

    # -- upstream lookups -------------------------------------------------
    def _cached(self, key: str, loader):
        """Memoise ``loader``, which returns ``(value, cacheable)``.

        A failed lookup is never cached. Caching one would keep a wrong answer
        for the whole TTL - and after a reboot, where the proxy can easily be
        up before Sonarr is, that wrong answer is an empty root folder set,
        which silently sends everything to the default instance.
        """
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        value, cacheable = loader()
        if cacheable:
            self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    def root_folder_paths(self, instance: Instance) -> set[str] | None:
        """Root folders on ``instance``, or ``None`` if they could not be read.

        The distinction matters: an unreadable instance is not an instance with
        no root folders, and treating it as one turns "I cannot see the anime
        server" into "this path belongs to the English server".
        """

        def load() -> tuple[set[str] | None, bool]:
            folders, ok, _status = self.upstream.fetch(
                instance, "/api/v3/rootfolder", headers=instance.auth_headers
            )
            if not ok or not isinstance(folders, list):
                return None, False
            return {
                str(f.get("path", "")).rstrip("/").lower()
                for f in folders
                if isinstance(f, dict) and f.get("path")
            }, True

        return self._cached(f"rootfolders:{instance.key}", load)

    # -- routing ----------------------------------------------------------
    def choose_for_add(self, payload: Any) -> tuple[str, str]:
        """Pick the target instance for an add/update payload from Seerr.

        Returns ``(instance_key, reason)``; the reason is logged so a
        misrouted title can be diagnosed from the container log alone.
        """
        if not isinstance(payload, dict):
            return ENGLISH, "no payload"

        offset = self.settings.id_offset
        for field in self.kind.add_id_hints:
            if field in payload and is_anime_id(payload.get(field), offset):
                return ANIME, f"{field} is in the anime ID range"

        raw_path = strip_prefix(payload.get("rootFolderPath", ""), self.settings.anime_label_prefix)
        path = str(raw_path or "").rstrip("/").lower()
        if path:
            anime_paths = self.root_folder_paths(self.service.anime)
            english_paths = self.root_folder_paths(self.service.english)
            if anime_paths is None or english_paths is None:
                # One side is unreadable, so ownership proves nothing: a path
                # "only on the English instance" may simply be one the anime
                # instance could not be asked about. Fall through to the
                # weaker rules rather than route on a false certainty.
                unreadable = (
                    self.service.anime if anime_paths is None else self.service.english
                )
                logger.warning(
                    "Cannot read root folders from %s, so root-folder routing is "
                    "unavailable for this request; falling back to seriesType and "
                    "the '%s' keyword",
                    unreadable.label,
                    self.settings.anime_path_match,
                )
            else:
                if path in anime_paths and path not in english_paths:
                    return ANIME, f"root folder {raw_path} exists only on the anime instance"
                if path in english_paths and path not in anime_paths:
                    return ENGLISH, f"root folder {raw_path} exists only on the english instance"

        hint_field = self.kind.type_hint_field
        if hint_field and str(payload.get(hint_field, "")).lower() == "anime":
            return ANIME, f"{hint_field} is anime"

        match = self.settings.anime_path_match
        if match and match in path:
            return ANIME, f"root folder {raw_path} matches '{match}'"

        return ENGLISH, "default instance"

    def instance_for_id(self, value: Any) -> tuple[Instance, Any]:
        key, real_id = decode_id(value, self.settings.id_offset)
        return self.service.instance_for(key), real_id

    def offset_for(self, instance: Instance) -> int:
        return self.settings.id_offset if instance.key == ANIME else 0

    # -- tags -------------------------------------------------------------
    def translate_tags(self, tags: Any, target: Instance, headers: dict[str, str]) -> Any:
        """Re-point tag IDs at ``target``, creating missing labels as needed.

        Seerr picks tags from the merged list, so an anime add can carry a tag
        that only exists on the English instance (or vice versa). Dropping it
        would silently lose user intent, so the label is resolved and recreated
        on the target instance instead.
        """
        if not isinstance(tags, list) or not tags:
            return tags

        offset = self.settings.id_offset
        resolved: list[int] = []
        for tag in tags:
            key, real_id = decode_id(tag, offset)
            if not isinstance(real_id, int):
                continue
            if key == target.key:
                resolved.append(real_id)
                continue
            source = self.service.instance_for(key)
            label = self._tag_label(source, real_id)
            if label is None:
                logger.warning("Dropping tag %s: no label found on %s", tag, source.label)
                continue
            mapped = self._tag_id_for_label(target, label, headers)
            if mapped is not None:
                resolved.append(mapped)
        return resolved

    def _tags(self, instance: Instance) -> list[dict]:
        def load() -> tuple[list[dict], bool]:
            tags, ok, _status = self.upstream.fetch(
                instance, "/api/v3/tag", headers=instance.auth_headers
            )
            if not ok or not isinstance(tags, list):
                return [], False
            return tags, True

        return self._cached(f"tags:{instance.key}", load)

    def _tag_label(self, instance: Instance, tag_id: int) -> str | None:
        for tag in self._tags(instance):
            if isinstance(tag, dict) and tag.get("id") == tag_id:
                return tag.get("label")
        return None

    def _tag_id_for_label(
        self, instance: Instance, label: str, headers: dict[str, str]
    ) -> int | None:
        for tag in self._tags(instance):
            if isinstance(tag, dict) and tag.get("label") == label:
                return tag.get("id")
        payload, status = self.upstream.json(
            instance, "POST", "/api/v3/tag", headers=headers, json_data={"label": label}
        )
        if status < 400 and isinstance(payload, dict):
            self._cache.pop(f"tags:{instance.key}", None)
            return payload.get("id")
        logger.warning(
            "Could not create tag '%s' on %s (HTTP %s); it will be dropped from this request",
            label,
            instance.label,
            status,
        )
        return None
