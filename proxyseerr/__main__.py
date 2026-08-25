"""Container entrypoint: serve every configured service on its own port."""
from __future__ import annotations

import logging
import sys
import threading

from waitress import create_server

from .app import create_app
from .config import ConfigError, Settings, load_settings
from .upstream import Upstream

logger = logging.getLogger("proxyseerr")


def probe_instances(settings: Settings) -> None:
    """Report reachability once at startup.

    Without this the first sign that an instance is unreachable is a request
    failing minutes later, or a library that silently comes back half empty.
    """
    upstream = Upstream(settings.timeout, settings.connect_timeout)
    for service in settings.services:
        for instance in service.instances:
            payload = upstream.json_or_empty(
                instance,
                "/api/v3/system/status",
                headers=instance.auth_headers,
                default={},
            )
            version = payload.get("version") if isinstance(payload, dict) else None
            if version:
                # The instance name is logged so that swapping the two URLs, or
                # restoring one from the wrong backup, is visible here rather
                # than as mysteriously misrouted titles later.
                logger.info(
                    "%s -> %s (%s, v%s)",
                    instance.label,
                    instance.url,
                    payload.get("instanceName") or "unnamed",
                    version,
                )
            else:
                logger.warning(
                    "%s -> %s is NOT responding. Requests routed there will fail and "
                    "merged reads will be missing its titles until it returns.",
                    instance.label,
                    instance.url,
                )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))

    if settings.allow_anonymous and not settings.proxy_api_key:
        logger.warning(
            "PROXY_ALLOW_ANONYMOUS is set: any client that can reach these ports can "
            "drive both of your servers. Only do this on a trusted network."
        )

    logger.info(
        "Authentication: %s | request log: %s | anime ID offset: %s",
        "API key required" if settings.proxy_api_key else "ANONYMOUS (no key required)",
        settings.request_log,
        settings.id_offset,
    )

    probe_instances(settings)

    servers = []
    for config in settings.services:
        app = create_app(settings, config)
        servers.append(create_server(app, host="0.0.0.0", port=config.port, threads=16))
        logger.info(
            "%s proxy listening on 0.0.0.0:%s (anime ID offset %s)",
            config.kind.name,
            config.port,
            settings.id_offset,
        )

    for server in servers[:-1]:
        threading.Thread(target=server.run, daemon=True).start()
    servers[-1].run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
