"""Request-level orchestration: merge, translate, forward."""
from __future__ import annotations

import logging
from typing import Any

from flask import Response, jsonify, request

from . import namespace as ns
from .config import Instance, Settings
from .routing import Router
from .upstream import Upstream, build_headers, clean_params, to_flask_response

logger = logging.getLogger(__name__)

JSON_CONTENT_TYPES = ("application/json", "text/json", "application/*+json")


class ProxyService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.upstream = Upstream(settings.timeout)
        self.router = Router(settings, self.upstream)

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
        return request.get_data(), False

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

        response = self.upstream.request(
            instance,
            method,
            path,
            headers=headers,
            params=params,
            json_data=json_data,
            data=data,
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

    def prepare_series_payload(self, payload: Any, target: Instance) -> Any:
        """Rewrite a Seerr add/update body into the target instance's ID space."""
        if not isinstance(payload, dict):
            return payload
        out = ns.decode_obj(payload, self.settings.id_offset, ns.SERIES_ID_FIELDS, ())
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
    def _fetch(self, instance: Instance, path: str, default: Any = None) -> Any:
        return self.upstream.json_or_empty(
            instance,
            path,
            headers=self.headers_for(instance),
            params=self.params_for(instance),
            default=default,
        )

    def merged_series(self) -> Response:
        english = self._fetch(self.settings.english, "/api/v3/series")
        anime = self._fetch(self.settings.anime, "/api/v3/series")
        english = english if isinstance(english, list) else []
        anime = anime if isinstance(anime, list) else []

        seen = {s.get("tvdbId") for s in english if isinstance(s, dict)}
        merged = list(english)
        for series in anime:
            if isinstance(series, dict) and series.get("tvdbId") in seen:
                logger.warning(
                    "tvdbId %s exists on both instances ('%s'); Seerr will see it twice",
                    series.get("tvdbId"),
                    series.get("title"),
                )
            merged.append(ns.encode_series(series, self.settings.id_offset))
        return jsonify(merged)

    def merged_lookup(self) -> Response:
        english = self._fetch(self.settings.english, "/api/v3/series/lookup")
        anime = self._fetch(self.settings.anime, "/api/v3/series/lookup")
        english = english if isinstance(english, list) else []
        anime = anime if isinstance(anime, list) else []

        merged = list(english)
        index = {
            s.get("tvdbId"): i
            for i, s in enumerate(merged)
            if isinstance(s, dict) and s.get("tvdbId")
        }
        for series in anime:
            if not isinstance(series, dict):
                continue
            encoded = ns.encode_series(series, self.settings.id_offset)
            tvdb_id = series.get("tvdbId")
            position = index.get(tvdb_id)
            if position is None:
                if tvdb_id:
                    index[tvdb_id] = len(merged)
                merged.append(encoded)
            elif series.get("id"):
                # Already added on the anime instance, so that copy wins: Seerr
                # must see the ID that actually resolves.
                merged[position] = encoded
        return jsonify(merged)

    def merged_list(self, path: str, *, prefix_anime: bool = False) -> Response:
        english = self._fetch(self.settings.english, path)
        anime = self._fetch(self.settings.anime, path)
        english = english if isinstance(english, list) else []
        anime = anime if isinstance(anime, list) else []

        encoded = ns.encode_list(anime, self.settings.id_offset)
        if prefix_anime:
            encoded = ns.prefix_labels(encoded, self.settings.anime_label_prefix)
        return jsonify(list(english) + list(encoded))

    def merged_queue(self, path: str) -> Response:
        english = self._fetch(self.settings.english, path, default=None)
        anime = self._fetch(self.settings.anime, path, default=None)
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
        encoded = ns.encode_obj(record, offset, ns.QUEUE_ID_FIELDS, ())
        if not isinstance(encoded, dict):
            return encoded
        if isinstance(encoded.get("series"), dict):
            encoded["series"] = ns.encode_series(encoded["series"], offset)
        if isinstance(encoded.get("episode"), dict):
            encoded["episode"] = ns.encode_obj(
                encoded["episode"], offset, ns.EPISODE_ID_FIELDS, ()
            )
        return encoded

    # -- health ------------------------------------------------------------
    def health(self) -> dict:
        report = {"status": "ok", "instances": {}}
        for instance in self.settings.instances:
            entry: dict[str, Any] = {"url": instance.url, "configured": instance.configured}
            payload = self.upstream.json_or_empty(
                instance,
                "/api/v3/system/status",
                headers={"X-Api-Key": instance.api_key},
                default={},
            )
            if isinstance(payload, dict) and payload.get("version"):
                entry["reachable"] = True
                entry["version"] = payload.get("version")
            else:
                entry["reachable"] = False
                report["status"] = "degraded"
            report["instances"][instance.key] = entry
        report["idOffset"] = self.settings.id_offset
        return report

    def json_body(self) -> Any:
        """The inbound JSON body, or ``None`` if the request had no JSON."""
        body, is_json = self._request_body()
        return body if is_json else None
