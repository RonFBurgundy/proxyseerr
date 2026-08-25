"""Container entrypoint: serve every configured service on its own port."""
from __future__ import annotations

import logging
import sys
import threading

from waitress import create_server

from .app import create_app
from .config import ConfigError, load_settings

logger = logging.getLogger("proxyseerr")


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

    servers = []
    for config in settings.services:
        app = create_app(settings, config)
        for instance in config.instances:
            logger.info("%s -> %s", instance.label, instance.url)
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
