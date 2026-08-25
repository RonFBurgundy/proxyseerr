"""Flask application: the Sonarr/Radarr v3 surface Seerr talks to."""
from __future__ import annotations

import hmac
import logging
import time

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import namespace as ns
from .config import ANIME, ENGLISH, Service, Settings
from .service import ProxyService
from .upstream import AllInstancesFailed, UpstreamError, redact

logger = logging.getLogger(__name__)

HEALTH_PATH = "/proxy/health"

# The only prefixes ever forwarded upstream. Seerr speaks nothing else, and a
# narrow surface keeps the proxy from being a general-purpose tunnel into a
# server that trusts it.
ALLOWED_PREFIXES = ("/api/", "/ping")


def safe_path(path: str) -> str | None:
    """Normalise an inbound path, or return ``None`` if it must not be forwarded."""
    candidate = "/" + path.lstrip("/")
    if any(ch in candidate for ch in ("\\", "\r", "\n")) or "\x00" in candidate:
        return None
    if ".." in candidate.split("/"):
        return None
    if not any(candidate == p or candidate.startswith(p) for p in ALLOWED_PREFIXES):
        return None
    return candidate


def decode_query_ids(args, params: tuple[str, ...], offset: int) -> tuple[str, dict]:
    """Decode namespaced IDs in the query string and report the owning instance."""
    target = ENGLISH
    overrides: dict[str, list[str]] = {}
    for key in params:
        if key not in args:
            continue
        decoded_values = []
        for value in args.getlist(key):
            parts = []
            for part in str(value).split(","):
                token = part.strip()
                if token.isdigit():
                    owner, real_id = ns.decode_id(int(token), offset)
                    if owner == ANIME:
                        target = ANIME
                    parts.append(str(real_id))
                else:
                    parts.append(part)
            decoded_values.append(",".join(parts))
        overrides[key] = decoded_values
    return target, overrides


def create_app(settings: Settings, service_config: Service) -> Flask:
    kind = service_config.kind
    resource = kind.resource

    app = Flask(f"proxyseerr.{kind.name}")
    app.config["MAX_CONTENT_LENGTH"] = settings.max_body_bytes
    service = ProxyService(settings, service_config)
    router = service.router
    app.extensions["proxyseerr"] = service

    def authenticated() -> bool:
        if not settings.proxy_api_key:
            return settings.allow_anonymous
        supplied = request.headers.get("X-Api-Key") or request.args.get("apikey") or ""
        return hmac.compare_digest(supplied, settings.proxy_api_key)

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        """Answer in JSON, like the API being proxied."""
        return jsonify({"error": exc.name}), exc.code

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        logger.exception(
            "Unhandled error serving %s %s", request.method, redact(request.path)
        )
        return jsonify({"error": "Internal proxy error"}), 500

    @app.errorhandler(UpstreamError)
    def handle_upstream_error(exc: UpstreamError):
        logger.error("%s", exc.log_message)
        return jsonify({"error": exc.public_message}), 502

    @app.errorhandler(AllInstancesFailed)
    def handle_total_failure(exc: AllInstancesFailed):
        logger.error("%s", exc.log_message)
        return jsonify({"error": exc.public_message}), 502

    @app.errorhandler(413)
    def handle_too_large(_exc):
        logger.warning(
            "Rejected oversized body on %s %s (limit %s bytes)",
            request.method,
            redact(request.path),
            settings.max_body_bytes,
        )
        return jsonify({"error": "Request body too large"}), 413

    @app.before_request
    def begin_request():
        g.request_started = time.perf_counter()
        g.upstream_label = None

    @app.before_request
    def enforce_api_key():
        if request.path == HEALTH_PATH:
            return None
        if not authenticated():
            logger.warning(
                "Rejected unauthenticated %s %s", request.method, redact(request.path)
            )
            return jsonify({"error": "Unauthorized"}), 401
        return None

    @app.after_request
    def harden(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        return response

    @app.after_request
    def log_request(response):
        """One line per request, so nothing reaches Seerr unrecorded.

        ``REQUEST_LOG=errors`` (the default) logs only the ones that went wrong;
        ``all`` logs every request at INFO; ``off`` disables it.
        """
        if settings.request_log == "off":
            return response
        failed = response.status_code >= 400
        if not failed and settings.request_log != "all":
            return response
        started = getattr(g, "request_started", None)
        elapsed = f"{(time.perf_counter() - started) * 1000:.0f}ms" if started else "?"
        logger.log(
            logging.WARNING if failed else logging.INFO,
            "%s %s -> %s via %s in %s",
            request.method,
            redact(request.full_path.rstrip("?")),
            response.status_code,
            getattr(g, "upstream_label", None) or "proxy",
            elapsed,
        )
        return response

    # -- proxy's own endpoints --------------------------------------------
    @app.get(HEALTH_PATH)
    def health():
        report = service.health(detailed=authenticated())
        return jsonify(report), 200 if report["status"] == "ok" else 503

    # -- library -----------------------------------------------------------
    @app.get(f"/api/v3/{resource}")
    def list_items():
        return service.merged_items()

    @app.get(f"/api/v3/{resource}/lookup")
    def lookup_items():
        return service.merged_lookup(f"/api/v3/{resource}/lookup")

    @app.get(f"/api/v3/{resource}/lookup/<string:variant>")
    def lookup_items_by_source(variant: str):
        if not variant.isalnum():
            return jsonify({"error": "Not found"}), 404
        return service.merged_lookup(f"/api/v3/{resource}/lookup/{variant}")

    @app.post(f"/api/v3/{resource}")
    def add_item():
        payload = service.json_body()
        key, reason = router.choose_for_add(payload)
        instance = service_config.instance_for(key)
        title = payload.get("title", "unknown") if isinstance(payload, dict) else "unknown"
        logger.info("Routing ADD '%s' to %s (%s)", title, instance.label, reason)
        return service.forward(
            instance,
            f"/api/v3/{resource}",
            json_data=service.prepare_item_payload(payload, instance),
            encode_fields=kind.item_id_fields,
            encode_list_fields=kind.item_list_fields,
        )

    @app.put(f"/api/v3/{resource}")
    def update_item():
        payload = service.json_body()
        item_id = payload.get("id") if isinstance(payload, dict) else None
        if item_id is None:
            key, _ = router.choose_for_add(payload)
            instance = service_config.instance_for(key)
        else:
            instance, _ = router.instance_for_id(item_id)
        return service.forward(
            instance,
            f"/api/v3/{resource}",
            json_data=service.prepare_item_payload(payload, instance),
            encode_fields=kind.item_id_fields,
            encode_list_fields=kind.item_list_fields,
        )

    @app.route(f"/api/v3/{resource}/<int:item_id>", methods=["GET", "PUT", "DELETE"])
    def item_by_id(item_id: int):
        instance, real_id = router.instance_for_id(item_id)
        payload = service.json_body() if request.method == "PUT" else None
        return service.forward(
            instance,
            f"/api/v3/{resource}/{real_id}",
            json_data=(
                service.prepare_item_payload(payload, instance) if payload is not None else None
            ),
            encode_fields=kind.item_id_fields,
            encode_list_fields=kind.item_list_fields,
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

    if kind.has_language_profiles:

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
        return service.forward(service_config.english, "/api/v3/tag")

    # -- queue -------------------------------------------------------------
    @app.get("/api/v3/queue")
    def queue():
        return service.merged_queue("/api/v3/queue")

    @app.get("/api/v3/queue/details")
    def queue_details():
        target, overrides = decode_query_ids(
            request.args, kind.query_id_params, settings.id_offset
        )
        if not overrides:
            return service.merged_queue("/api/v3/queue/details")
        return service.forward(
            service_config.instance_for(target),
            "/api/v3/queue/details",
            query_overrides=overrides,
            encode_fields=kind.queue_id_fields,
        )

    # -- commands ----------------------------------------------------------
    @app.post("/api/v3/command")
    def command():
        payload = service.json_body()
        offset = settings.id_offset
        target = ENGLISH
        has_target = False
        if isinstance(payload, dict):
            for field in kind.command_id_fields:
                if field == "id" or field not in payload:
                    continue
                has_target = True
                if ns.is_anime_id(payload[field], offset):
                    target = ANIME
            for field in kind.command_list_fields:
                values = payload.get(field)
                if isinstance(values, list) and values:
                    has_target = True
                    if any(ns.is_anime_id(v, offset) for v in values):
                        target = ANIME

        name = payload.get("name", "unknown") if isinstance(payload, dict) else "unknown"
        if not has_target:
            logger.info("Broadcasting command '%s' to both %s instances", name, kind.name)
            return broadcast_command(payload)

        instance = service_config.instance_for(target)
        logger.info("Routing command '%s' to %s", name, instance.label)
        return service.forward(
            instance,
            "/api/v3/command",
            json_data=service.decode_payload(
                payload, kind.command_id_fields, kind.command_list_fields
            ),
            encode_fields=kind.command_id_fields,
            encode_list_fields=kind.command_list_fields,
        )

    def broadcast_command(payload):
        results = {}
        for instance in service_config.instances:
            try:
                body, status = service.upstream.json(
                    instance,
                    "POST",
                    "/api/v3/command",
                    headers=service.headers_for(instance),
                    json_data=payload,
                )
            except UpstreamError as exc:
                logger.warning("%s", exc.log_message)
                continue
            results[instance.key] = (
                service.encode_payload(
                    body,
                    kind.command_id_fields,
                    kind.command_list_fields,
                    router.offset_for(instance),
                ),
                status,
            )
        for key in (ENGLISH, ANIME):
            if key in results:
                return jsonify(results[key][0]), results[key][1]
        return jsonify({"error": "No instance accepted the command"}), 502

    @app.route("/api/v3/command/<int:command_id>", methods=["GET", "DELETE"])
    def command_by_id(command_id: int):
        instance, real_id = router.instance_for_id(command_id)
        return service.forward(
            instance,
            f"/api/v3/command/{real_id}",
            encode_fields=kind.command_id_fields,
            encode_list_fields=kind.command_list_fields,
        )

    # -- everything else ---------------------------------------------------
    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def catch_all(path: str):
        clean = safe_path(path)
        if clean is None:
            logger.warning("Refusing to forward path %s", redact(path))
            return jsonify({"error": "Not found"}), 404

        offset = settings.id_offset
        target, overrides = decode_query_ids(request.args, kind.query_id_params, offset)

        segments = clean.split("/")
        if segments[-1].isdigit():
            owner, real_id = ns.decode_id(int(segments[-1]), offset)
            if owner == ANIME:
                target = ANIME
            segments[-1] = str(real_id)
            clean = "/".join(segments)

        return service.forward(
            service_config.instance_for(target),
            clean,
            query_overrides=overrides,
            encode_fields=kind.catch_all_id_fields,
            encode_list_fields=kind.catch_all_list_fields,
        )

    return app
