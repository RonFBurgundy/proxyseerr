"""Container healthcheck.

The container is healthy when the proxy is answering on every port it was
configured to serve. An upstream being down is reported as HTTP 503 by
``/proxy/health`` and is deliberately *not* treated as container failure -
restarting the proxy would not bring Sonarr back.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

from .config import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError:
        return 1
    for service in settings.services:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{service.port}/proxy/health", timeout=5
            )
        except urllib.error.HTTPError:
            continue
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
