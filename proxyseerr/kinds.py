"""What differs between a Sonarr pair and a Radarr pair.

Everything else in the proxy is written against this descriptor, so the two
services share one routing and namespacing implementation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaKind:
    name: str
    resource: str
    external_id: str
    item_id_fields: tuple[str, ...]
    item_list_fields: tuple[str, ...]
    command_id_fields: tuple[str, ...]
    command_list_fields: tuple[str, ...]
    queue_id_fields: tuple[str, ...]
    query_id_params: tuple[str, ...]
    catch_all_id_fields: tuple[str, ...]
    catch_all_list_fields: tuple[str, ...]
    has_language_profiles: bool
    type_hint_field: str | None
    default_port: int

    @property
    def collection_path(self) -> str:
        return f"/api/v3/{self.resource}"

    @property
    def add_id_hints(self) -> tuple[str, ...]:
        """Payload fields whose value alone identifies the owning instance."""
        return self.item_id_fields + ("rootFolderId",)


SONARR = MediaKind(
    name="sonarr",
    resource="series",
    external_id="tvdbId",
    item_id_fields=("id", "qualityProfileId", "languageProfileId"),
    item_list_fields=("tags",),
    command_id_fields=("id", "seriesId", "episodeFileId"),
    command_list_fields=("seriesIds", "episodeIds", "episodeFileIds"),
    queue_id_fields=("id", "seriesId", "episodeId"),
    query_id_params=("seriesId", "episodeId", "episodeFileId", "seriesIds", "episodeIds"),
    catch_all_id_fields=("id", "seriesId", "episodeId", "episodeFileId"),
    catch_all_list_fields=("seriesIds", "episodeIds", "episodeFileIds", "tags"),
    has_language_profiles=True,
    type_hint_field="seriesType",
    default_port=5000,
)

RADARR = MediaKind(
    name="radarr",
    resource="movie",
    external_id="tmdbId",
    item_id_fields=("id", "qualityProfileId"),
    item_list_fields=("tags",),
    command_id_fields=("id", "movieId", "movieFileId"),
    command_list_fields=("movieIds", "movieFileIds"),
    queue_id_fields=("id", "movieId"),
    query_id_params=("movieId", "movieFileId", "movieIds"),
    catch_all_id_fields=("id", "movieId", "movieFileId"),
    catch_all_list_fields=("movieIds", "movieFileIds", "tags"),
    has_language_profiles=False,
    type_hint_field=None,
    default_port=5001,
)

KINDS = {SONARR.name: SONARR, RADARR.name: RADARR}
