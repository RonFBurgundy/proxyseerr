"""Flask application: the Sonarr v3 surface Seerr talks to."""
from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from . import namespace as ns
from .config import Settings, load_settings
from .service import ProxyService
from .upstream import UpstreamError

logger = logging.getLogger(__name__)

# Query parameters whose values live in the merged ID space.
ID_QUERY_PARAMS = ("seriesId", "episodeId", "episodeFileId", "seriesIds", "episodeIds")

CATCH_ALL_ID_FIELDS = ns.PLAIN_ID_FIELDS + ("seriesId", "episodeId", "episodeFileId")
CATCH_ALL_LIST_FIELDS = ("seriesIds", "episodeIds", "episodeFileIds", "tags")

HEALTH_PATH = "/proxy/health"


def decode_query_ids(args, offset: int) -> tuple[str, dict[str, list[str]]]:
    """Decode namespaced IDs in the query string and report the owning instance."""
    target = ns.ENGLISH
    overrides: dict[str, list[str]] = {}
    for key in ID_QUERY_PARAMS:
        if key not in args:
            continue
        decoded_values = []
        for value in args.getlist(key):
            parts = []
            for part in str(value).split(","):
                token = part.strip()
                if token.isdigit():
                    owner, real_id = ns.decode_id(int(token), offset)
                    if owner == ns.ANIME:
                        target = ns.ANIME
                    parts.append(str(real_id))
                else:
                    parts.append(part)
            decoded_values.append(",".join(parts))
        overrides[key] = decoded_values
    return target, overrides


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = Flask(__name__)
    service = ProxyService(settings)
    router = service.router
    app.extensions["proxyseerr"] = service

    @app.errorhandler(UpstreamError)
    def handle_upstream_error(exc: UpstreamError):
        logger.error("%s", exc)
        return jsonify({"error": "Upstream connection failed", "details": str(exc)}), 502

    @app.before_request
    def enforce_api_key():
        if not settings.proxy_api_key or request.path == HEALTH_PATH:
            return None
        supplied = request.headers.get("X-Api-Key") or request.args.get("apikey")
        if supplied != settings.proxy_api_key:
            return jsonify({"error": "Unauthorized"}), 401
        return None

    # -- proxy's own endpoints --------------------------------------------
    @app.get(HEALTH_PATH)
    def health():
        report = service.health()
        return jsonify(report), 200 if report["status"] == "ok" else 503

    # -- series ------------------------------------------------------------
    @app.get("/api/v3/series")
    def list_series():
        return service.merged_series()

    @app.get("/api/v3/series/lookup")
    def lookup_series():
        return service.merged_lookup()

    @app.post("/api/v3/series")
    def add_series():
        payload = service.json_body()
        key, reason = router.choose_for_add(payload)
        instance = settings.instance_for(key)
        title = payload.get("title", "unknown") if isinstance(payload, dict) else "unknown"
        logger.info("Routing ADD '%s' to %s Sonarr (%s)", title, instance.label, reason)
        return service.forward(
            instance,
            "/api/v3/series",
            json_data=service.prepare_series_payload(payload, instance),
            encode_fields=ns.SERIES_ID_FIELDS,
            encode_list_fields=ns.SERIES_LIST_FIELDS,
        )

    @app.put("/api/v3/series")
    def update_series():
        payload = service.json_body()
        series_id = payload.get("id") if isinstance(payload, dict) else None
        if series_id is None:
            key, _ = router.choose_for_add(payload)
            instance = settings.instance_for(key)
        else:
            instance, _ = router.instance_for_id(series_id)
        return service.forward(
            instance,
            "/api/v3/series",
            json_data=service.prepare_series_payload(payload, instance),
            encode_fields=ns.SERIES_ID_FIELDS,
            encode_list_fields=ns.SERIES_LIST_FIELDS,
        )

    @app.route("/api/v3/series/<int:series_id>", methods=["GET", "PUT", "DELETE"])
    def series_by_id(series_id: int):
        instance, real_id = router.instance_for_id(series_id)
        payload = service.json_body() if request.method == "PUT" else None
        return service.forward(
            instance,
            f"/api/v3/series/{real_id}",
            json_data=(
                service.prepare_series_payload(payload, instance) if payload is not None else None
            ),
            encode_fields=ns.SERIES_ID_FIELDS,
            encode_list_fields=ns.SERIES_LIST_FIELDS,
        )

    # -- dropdown sources --------------------------------------------------
    @app.get("/api/v3/rootfolder")
    def root_folders():
        return service.merged_list("/api/v3/rootfolder")

    @app.get("/api/v3/qualityprofile")
    def quality_profiles():
        return service.merged_list("/api/v3/qualityprofile", prefix_anime=True)

    @app.get("/api/v3/profile")
    def legacy_profiles():
        return service.merged_list("/api/v3/profile", prefix_anime=True)

    @app.get("/api/v3/languageprofile")
    def language_profiles():
        return service.merged_list("/api/v3/languageprofile", prefix_anime=True)

    @app.get("/api/v3/tag")
    def tags():
        return service.merged_list("/api/v3/tag")

    @app.post("/api/v3/tag")
    def create_tag():
        # Created on the default instance only; the anime side gets the same
        # label on demand when a request that carries the tag is routed there.
        router.invalidate()
        return service.forward(settings.english, "/api/v3/tag")

    # -- queue -------------------------------------------------------------
    @app.get("/api/v3/queue")
    def queue():
        return service.merged_queue("/api/v3/queue")

    @app.get("/api/v3/queue/details")
    def queue_details():
        target, overrides = decode_query_ids(request.args, settings.id_offset)
        if not overrides:
            return service.merged_queue("/api/v3/queue/details")
        instance = settings.instance_for(target)
        return service.forward(
            instance,
            "/api/v3/queue/details",
            query_overrides=overrides,
            encode_fields=ns.QUEUE_ID_FIELDS,
        )

    # -- commands ----------------------------------------------------------
    @app.post("/api/v3/command")
    def command():
        payload = service.json_body()
        offset = settings.id_offset
        target = ns.ENGLISH
        has_target = False
        if isinstance(payload, dict):
            for field in ns.COMMAND_ID_FIELDS:
                if field == "id" or field not in payload:
                    continue
                has_target = True
                if ns.is_anime_id(payload[field], offset):
                    target = ns.ANIME
            for field in ns.COMMAND_LIST_FIELDS:
                values = payload.get(field)
                if isinstance(values, list) and values:
                    has_target = True
                    if any(ns.is_anime_id(v, offset) for v in values):
                        target = ns.ANIME

        name = payload.get("name", "unknown") if isinstance(payload, dict) else "unknown"
        if not has_target:
            logger.info("Broadcasting command '%s' to both instances", name)
            return broadcast_command(payload)

        instance = settings.instance_for(target)
        logger.info("Routing command '%s' to %s Sonarr", name, instance.label)
        return service.forward(
            instance,
            "/api/v3/command",
            json_data=service.decode_payload(
                payload, ns.COMMAND_ID_FIELDS, ns.COMMAND_LIST_FIELDS
            ),
            encode_fields=ns.COMMAND_ID_FIELDS,
            encode_list_fields=ns.COMMAND_LIST_FIELDS,
        )

    def broadcast_command(payload):
        results = {}
        for instance in settings.instances:
            try:
                body, status = service.upstream.json(
                    instance,
                    "POST",
                    "/api/v3/command",
                    headers=service.headers_for(instance),
                    json_data=payload,
                )
            except UpstreamError as exc:
                logger.warning("%s", exc)
                continue
            offset = router.offset_for(instance)
            results[instance.key] = (
                service.encode_payload(body, ns.COMMAND_ID_FIELDS, ns.COMMAND_LIST_FIELDS, offset),
                status,
            )
        if ns.ENGLISH in results:
            return jsonify(results[ns.ENGLISH][0]), results[ns.ENGLISH][1]
        if ns.ANIME in results:
            return jsonify(results[ns.ANIME][0]), results[ns.ANIME][1]
        return jsonify({"error": "No Sonarr instance accepted the command"}), 502

    @app.route("/api/v3/command/<int:command_id>", methods=["GET", "DELETE"])
    def command_by_id(command_id: int):
        instance, real_id = router.instance_for_id(command_id)
        return service.forward(
            instance,
            f"/api/v3/command/{real_id}",
            encode_fields=ns.COMMAND_ID_FIELDS,
            encode_list_fields=ns.COMMAND_LIST_FIELDS,
        )

    # -- everything else ---------------------------------------------------
    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def catch_all(path: str):
        offset = settings.id_offset
        target, overrides = decode_query_ids(request.args, offset)

        segments = path.split("/")
        if segments and segments[-1].isdigit():
            owner, real_id = ns.decode_id(int(segments[-1]), offset)
            if owner == ns.ANIME:
                target = ns.ANIME
            segments[-1] = str(real_id)
            path = "/".join(segments)

        instance = settings.instance_for(target)
        return service.forward(
            instance,
            f"/{path}",
            query_overrides=overrides,
            encode_fields=CATCH_ALL_ID_FIELDS,
            encode_list_fields=CATCH_ALL_LIST_FIELDS,
        )

    return app
