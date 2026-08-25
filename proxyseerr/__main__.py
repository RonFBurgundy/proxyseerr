"""Container entrypoint: serve the proxy with waitress."""
from __future__ import annotations

import logging

from waitress import serve

from .app import create_app
from .config import load_settings

logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    for instance in settings.instances:
        if not instance.api_key:
            logger.warning(
                "%s Sonarr has no API key set; requests to it will be rejected", instance.label
            )
        logger.info("%s Sonarr -> %s", instance.label, instance.url)
    logger.info(
        "proxyseerr listening on 0.0.0.0:%s (anime ID offset %s)",
        settings.port,
        settings.id_offset,
    )
    serve(app, host="0.0.0.0", port=settings.port, threads=16, ident="proxyseerr")


if __name__ == "__main__":
    main()
