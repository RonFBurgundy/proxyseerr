"""Deciding which Sonarr instance a request belongs to."""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import Instance, Settings
from .namespace import ANIME, ENGLISH, decode_id, is_anime_id, strip_prefix
from .upstream import Upstream

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60

# Payload fields whose value is a namespaced ID that already identifies an
# instance on its own.
ADD_ID_HINTS = ("qualityProfileId", "languageProfileId", "rootFolderId", "id")


class Router:
    def __init__(self, settings: Settings, upstream: Upstream):
        self.settings = settings
        self.upstream = upstream
        self._cache: dict[str, tuple[float, Any]] = {}

    # -- upstream lookups -------------------------------------------------
    def _cached(self, key: str, loader):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    def root_folder_paths(self, instance: Instance) -> set[str]:
        def load() -> set[str]:
            folders = self.upstream.json_or_empty(instance, "/api/v3/rootfolder")
            return {
                str(f.get("path", "")).rstrip("/").lower()
                for f in folders
                if isinstance(f, dict) and f.get("path")
            }

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
        for field in ADD_ID_HINTS:
            if field in payload and is_anime_id(payload.get(field), offset):
                return ANIME, f"{field} is in the anime ID range"

        raw_path = strip_prefix(payload.get("rootFolderPath", ""), self.settings.anime_label_prefix)
        path = str(raw_path or "").rstrip("/").lower()
        if path:
            anime_paths = self.root_folder_paths(self.settings.anime)
            english_paths = self.root_folder_paths(self.settings.english)
            if path in anime_paths and path not in english_paths:
                return ANIME, f"root folder {raw_path} exists only on the anime instance"
            if path in english_paths and path not in anime_paths:
                return ENGLISH, f"root folder {raw_path} exists only on the english instance"

        if str(payload.get("seriesType", "")).lower() == "anime":
            return ANIME, "seriesType is anime"

        match = self.settings.anime_path_match
        if match and match in path:
            return ANIME, f"root folder {raw_path} matches '{match}'"

        return ENGLISH, "default instance"

    def instance_for_id(self, value: Any) -> tuple[Instance, Any]:
        key, real_id = decode_id(value, self.settings.id_offset)
        return self.settings.instance_for(key), real_id

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
            source = self.settings.instance_for(key)
            label = self._tag_label(source, real_id)
            if label is None:
                logger.warning(
                    "Dropping tag %s: no label found on %s Sonarr", tag, source.label
                )
                continue
            mapped = self._tag_id_for_label(target, label, headers)
            if mapped is not None:
                resolved.append(mapped)
        return resolved

    def _tags(self, instance: Instance) -> list[dict]:
        return self._cached(
            f"tags:{instance.key}",
            lambda: self.upstream.json_or_empty(instance, "/api/v3/tag"),
        )

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
            "Could not create tag '%s' on %s Sonarr (HTTP %s)", label, instance.label, status
        )
        return None
