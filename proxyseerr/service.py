"""Request-level orchestration: merge, translate, forward."""
from __future__ import annotations

import logging
from typing import Any

from flask import Response, g, jsonify, request

from . import namespace as ns
from .config import ANIME, ENGLISH, Instance, Service, Settings
from .routing import Router
from .upstream import (
    AllInstancesFailed,
    Upstream,
    build_headers,
    clean_params,
    error_detail,
    redact,
    to_flask_response,
)

logger = logging.getLogger(__name__)

JSON_CONTENT_TYPES = ("application/json", "text/json", "application/*+json")


class ProxyService:
    def __init__(self, settings: Settings, service: Service):
        self.settings = settings
        self.service = service
        self.kind = service.kind
        self.upstream = Upstream(settings.timeout, settings.connect_timeout)
        self.router = Router(settings, service, self.upstream)

    # -- request plumbing -------------------------------------------------
    def headers_for(self, instance: Instance) -> dict[str, str]:
        return build_headers(request.headers, instance)

    def params_for(self, instance: Instance, overrides: dict[str, Any] | None = None) -> dict:
        params = clean_params(request.args.to_dict(flat=False), instance)
        if overrides:
            params.update(overrides)
        return params

    def _request_body(self) -> tuple[Any, bool]:
        """Return ``(payload, is_json)`` for the inbound request body."""
        if not request.data and not request.form:
            return None, False
        content_type = (request.content_type or "").lower()
        if any(kind.rstrip("*") in content_type for kind in JSON_CONTENT_TYPES):
            payload = request.get_json(silent=True)
            if payload is not None:
                return payload, True
            logger.warning(
                "%s %s declared %s but the body is not valid JSON; forwarding it "
                "verbatim and skipping ID translation",
                request.method,
                redact(request.path),
                content_type,
            )
        return request.get_data(), False

    def json_body(self) -> Any:
        """The inbound JSON body, or ``None`` if the request had no JSON."""
        body, is_json = self._request_body()
        return body if is_json else None

    def forward(
        self,
        instance: Instance,
        path: str,
        *,
        method: str | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        query_overrides: dict[str, Any] | None = None,
        encode_fields: tuple[str, ...] = ns.PLAIN_ID_FIELDS,
        encode_list_fields: tuple[str, ...] = (),
        decode_body: bool = True,
    ) -> Response:
        """Send one request upstream and translate the reply back into proxy IDs."""
        method = method or request.method
        headers = self.headers_for(instance)
        if params is None:
            params = self.params_for(instance, query_overrides)

        data = None
        if json_data is None:
            body, is_json = self._request_body()
            if is_json:
                json_data = (
                    self.decode_payload(body, encode_fields, encode_list_fields)
                    if decode_body
                    else body
                )
            else:
                data = body

        g.upstream_label = instance.label
        response = self.upstream.request(
            instance,
            method,
            path,
            headers=headers,
            params=params,
            json_data=json_data,
            data=data,
        )
        if response.status_code >= 400:
            # Passed through to Seerr as-is, but never silently: this is where a
            # rejected add or a wrong-instance lookup shows up.
            logger.warning(
                "%s rejected %s %s with HTTP %s: %s",
                instance.label,
                method,
                redact(path),
                response.status_code,
                error_detail(response),
            )

        offset = self.router.offset_for(instance)
        if offset == 0:
            return to_flask_response(response)

        try:
            payload = response.json()
        except ValueError:
            return to_flask_response(response)
        encoded = self.encode_payload(payload, encode_fields, encode_list_fields, offset)
        return jsonify(encoded), response.status_code

    # -- payload translation ----------------------------------------------
    def encode_payload(
        self,
        payload: Any,
        fields: tuple[str, ...],
        list_fields: tuple[str, ...],
        offset: int,
    ) -> Any:
        if offset == 0:
            return payload
        if isinstance(payload, list):
            return [ns.encode_obj(item, offset, fields, list_fields) for item in payload]
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            out = dict(payload)
            out["records"] = [
                ns.encode_obj(item, offset, fields, list_fields) for item in payload["records"]
            ]
            return out
        return ns.encode_obj(payload, offset, fields, list_fields)

    def decode_payload(
        self, payload: Any, fields: tuple[str, ...], list_fields: tuple[str, ...]
    ) -> Any:
        offset = self.settings.id_offset
        if isinstance(payload, list):
            return [ns.decode_obj(item, offset, fields, list_fields) for item in payload]
        return ns.decode_obj(payload, offset, fields, list_fields)

    def prepare_item_payload(self, payload: Any, target: Instance) -> Any:
        """Rewrite a Seerr add/update body into the target instance's ID space."""
        if not isinstance(payload, dict):
            return payload
        out = ns.decode_obj(payload, self.settings.id_offset, self.kind.item_id_fields, ())
        if "rootFolderPath" in out:
            out["rootFolderPath"] = ns.strip_prefix(
                out["rootFolderPath"], self.settings.anime_label_prefix
            )
        if isinstance(out.get("tags"), list):
            out["tags"] = self.router.translate_tags(
                out["tags"], target, self.headers_for(target)
            )
        return out

    # -- merged reads ------------------------------------------------------
    def _expect_list(self, payload: Any, instance: Instance, path: str) -> list:
        if isinstance(payload, list):
            return payload
        if payload is not None and payload != {}:
            logger.warning(
                "%s answered %s with %s, expected a list; treating as empty",
                instance.label,
                redact(path),
                type(payload).__name__,
            )
        return []

    def _fetch_both(self, path: str) -> tuple[Any, Any]:
        """Read ``path`` from both instances.

        Returns each payload, or ``None`` for an instance that did not answer.
        If neither answered the caller gets an error rather than a merged empty
        result, so a total outage can never look like an empty library.
        """
        english, english_ok, english_status = self.upstream.fetch(
            self.service.english,
            path,
            headers=self.headers_for(self.service.english),
            params=self.params_for(self.service.english),
        )
        anime, anime_ok, anime_status = self.upstream.fetch(
            self.service.anime,
            path,
            headers=self.headers_for(self.service.anime),
            params=self.params_for(self.service.anime),
        )
        g.upstream_label = "both instances"
        if not english_ok and not anime_ok:
            agreed = english_status if english_status == anime_status else None
            raise AllInstancesFailed(path, agreed)
        if not english_ok or not anime_ok:
            missing = self.service.english if not english_ok else self.service.anime
            logger.warning(
                "Answering %s without %s's data; the result is incomplete",
                redact(path),
                missing.label,
            )
        return english, anime

    def merged_items(self) -> Response:
        path = self.kind.collection_path
        external_id = self.kind.external_id
        english_raw, anime_raw = self._fetch_both(path)
        english = self._expect_list(english_raw, self.service.english, path)
        anime = self._expect_list(anime_raw, self.service.anime, path)

        seen = {item.get(external_id) for item in english if isinstance(item, dict)}
        merged = list(english)
        for item in anime:
            if isinstance(item, dict) and item.get(external_id) in seen:
                logger.warning(
                    "%s %s exists on both %s instances ('%s'); Seerr will see it twice",
                    external_id,
                    item.get(external_id),
                    self.kind.name,
                    item.get("title"),
                )
            merged.append(ns.encode_item(item, self.kind, self.settings.id_offset))
        return jsonify(merged)

    def merged_lookup(self, path: str) -> Response:
        english, anime = self._fetch_both(path)
        offset = self.settings.id_offset
        external_id = self.kind.external_id

        # Radarr's /movie/lookup/tmdb answers with a bare object, not a list.
        if isinstance(english, dict) or isinstance(anime, dict):
            if isinstance(anime, dict) and anime.get("id"):
                return jsonify(ns.encode_item(anime, self.kind, offset))
            return jsonify(english if english is not None else anime)

        merged = list(english) if isinstance(english, list) else []
        anime_items = anime if isinstance(anime, list) else []
        index = {
            item.get(external_id): i
            for i, item in enumerate(merged)
            if isinstance(item, dict) and item.get(external_id)
        }
        for item in anime_items:
            if not isinstance(item, dict):
                continue
            encoded = ns.encode_item(item, self.kind, offset)
            key = item.get(external_id)
            position = index.get(key)
            if position is None:
                if key:
                    index[key] = len(merged)
                merged.append(encoded)
            elif item.get("id"):
                # Already added on the anime instance, so that copy wins: Seerr
                # must see the ID that actually resolves.
                merged[position] = encoded
        return jsonify(merged)

    def merged_list(self, path: str, *, prefix_anime: bool = False) -> Response:
        english_raw, anime_raw = self._fetch_both(path)
        english = self._expect_list(english_raw, self.service.english, path)
        anime = self._expect_list(anime_raw, self.service.anime, path)

        encoded = ns.encode_list(anime, self.settings.id_offset)
        if prefix_anime:
            encoded = ns.prefix_labels(encoded, self.settings.anime_label_prefix)
        return jsonify(list(english) + list(encoded))

    def merged_queue(self, path: str) -> Response:
        english, anime = self._fetch_both(path)
        offset = self.settings.id_offset

        def records(payload: Any) -> list:
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict) and isinstance(payload.get("records"), list):
                return payload["records"]
            return []

        encoded_anime = [self._encode_queue_record(r, offset) for r in records(anime)]
        merged = records(english) + encoded_anime

        if isinstance(english, dict) and "records" in english:
            out = dict(english)
            out["records"] = merged
            out["totalRecords"] = len(merged)
            return jsonify(out)
        return jsonify(merged)

    def _encode_queue_record(self, record: Any, offset: int) -> Any:
        encoded = ns.encode_obj(record, offset, self.kind.queue_id_fields, ())
        if not isinstance(encoded, dict):
            return encoded
        for nested in ("series", "movie"):
            if isinstance(encoded.get(nested), dict):
                encoded[nested] = ns.encode_item(encoded[nested], self.kind, offset)
        if isinstance(encoded.get("episode"), dict):
            encoded["episode"] = ns.encode_obj(
                encoded["episode"], offset, ("id", "seriesId", "episodeFileId"), ()
            )
        return encoded

    # -- health ------------------------------------------------------------
    def health(self, detailed: bool) -> dict:
        """Reachability of both instances.

        Without a valid API key the report is deliberately thin: internal URLs
        and versions are not something an unauthenticated caller should learn.
        """
        report: dict[str, Any] = {"service": self.kind.name, "status": "ok", "instances": {}}
        for instance in self.service.instances:
            payload = self.upstream.json_or_empty(
                instance,
                "/api/v3/system/status",
                headers={"X-Api-Key": instance.api_key},
                default={},
            )
            reachable = bool(isinstance(payload, dict) and payload.get("version"))
            entry: dict[str, Any] = {"reachable": reachable}
            if detailed:
                entry["url"] = instance.url
                entry["version"] = payload.get("version") if reachable else None
            if not reachable:
                report["status"] = "degraded"
            report["instances"][instance.key] = entry
        if detailed:
            report["idOffset"] = self.settings.id_offset
        return report
