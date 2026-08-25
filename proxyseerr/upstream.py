"""HTTP plumbing for talking to the Sonarr/Radarr backends."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from flask import Response
from requests.cookies import RequestsCookieJar

from .config import Instance

logger = logging.getLogger(__name__)

# Only these request headers are relayed upstream. An allowlist keeps anything
# the caller attaches - cookies, Authorization, forwarding headers - from
# reaching a server that trusts this proxy.
FORWARDED_REQUEST_HEADERS = frozenset({"content-type", "accept", "user-agent"})

# Likewise outbound: no Set-Cookie, no auth challenges, no upstream banners.
FORWARDED_RESPONSE_HEADERS = frozenset(
    {"content-type", "cache-control", "date", "etag", "expires", "last-modified", "vary"}
)

_SECRET_PATTERN = re.compile(r"(?i)((?:api[_-]?key|apikey|token|password)=)[^&\s'\"]+")


def redact(text: Any) -> str:
    """Strip credentials out of anything headed for a log line."""
    return _SECRET_PATTERN.sub(r"\1<redacted>", str(text))


def error_detail_from(payload: Any, limit: int = 300) -> str:
    """A short, redacted description of an error payload."""
    if isinstance(payload, list):
        messages = [
            str(item.get("errorMessage") or item.get("message"))
            for item in payload
            if isinstance(item, dict) and (item.get("errorMessage") or item.get("message"))
        ]
        if messages:
            return redact("; ".join(messages))[:limit]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return redact(str(message))[:limit]
    if payload is None:
        return "<no JSON body>"
    return redact(str(payload))[:limit]


def error_detail(response: Any, limit: int = 300) -> str:
    """Explain an upstream error response.

    Sonarr and Radarr say why they rejected a request in the body ("Invalid
    quality profile", "Root folder does not exist"). Passing that through to
    Seerr without recording it is how an add fails with nothing in the log.
    """
    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return redact(text[:limit]) if text else "<empty body>"
    return error_detail_from(payload, limit)


class AllInstancesFailed(RuntimeError):
    """Every instance failed a merged read.

    Answering with an empty list here would tell Seerr the library is empty,
    which is worse than an error: it can mark everything unavailable. A 502
    makes Seerr keep what it already knows.
    """

    def __init__(self, path: str, status: int | None = None, missing: str | None = None):
        # A status both instances agree on is their answer, not an outage:
        # Sonarr v4 has no /languageprofile, so both legitimately 404.
        self.status_code = status if status and 400 <= status < 500 else 502
        if missing:
            self.public_message = f"{missing} could not be reached"
            self.log_message = (
                f"Refusing to answer {redact(path)} without {missing}'s data; "
                f"returning {self.status_code}"
            )
        elif self.status_code != 502:
            self.public_message = f"Both instances returned HTTP {status}"
            self.log_message = (
                f"Both instances rejected {redact(path)} with HTTP {status}; "
                f"passing it through"
            )
        else:
            self.public_message = "No instance could be reached"
            self.log_message = (
                f"Every instance failed for {redact(path)}; returning {self.status_code}"
            )
        super().__init__(self.log_message)


class UpstreamError(RuntimeError):
    """An upstream server could not be reached.

    ``log_message`` names the instance and URL for the operator; ``public_message``
    is what a caller is allowed to see, so internal topology is not echoed back.
    """

    def __init__(self, instance: Instance, exc: Exception):
        self.instance = instance
        self.original = exc
        self.log_message = redact(f"{instance.label} unreachable at {instance.url}: {exc}")
        self.public_message = f"{instance.label} did not respond"
        super().__init__(self.log_message)


def build_headers(incoming: Any, instance: Instance) -> dict[str, str]:
    headers = {
        key: value
        for key, value in (incoming or {})
        if key.lower() in FORWARDED_REQUEST_HEADERS
    }
    headers["X-Api-Key"] = instance.api_key
    return headers


def clean_params(params: dict[str, Any], instance: Instance) -> dict[str, Any]:
    """Swap any inline apikey for the target instance's own key."""
    out = dict(params or {})
    for key in list(out):
        if key.lower() == "apikey":
            out[key] = instance.api_key
    return out


class _NoCookieJar(RequestsCookieJar):
    """A jar that never stores or replays a cookie.

    One session serves both instances, and a cookie's domain ignores the port -
    so with the usual layout of two instances on one host, a cookie set by one
    would be sent to the other. Nothing here needs cookies: every request
    authenticates with X-Api-Key.
    """

    def set_cookie(self, *args, **kwargs) -> None:
        return None

    def extract_cookies(self, *args, **kwargs) -> None:
        return None


class Upstream:
    def __init__(
        self,
        timeout: float,
        connect_timeout: float | None = None,
        pool_size: int = 32,
        slow_after: float | None = None,
    ):
        self.timeout = timeout
        # A wrong host or port should fail in seconds, not hold a thread for the
        # full read budget; a real read on a spun-down array legitimately needs it.
        self.connect_timeout = min(connect_timeout or 5.0, timeout)
        # Warn well before the limit, so a slow instance is visible as a warning
        # rather than only as an eventual timeout.
        self.slow_after = slow_after if slow_after is not None else max(1.0, timeout / 2)
        self.session = requests.Session()
        self.session.cookies = _NoCookieJar()
        # waitress serves 16 threads per port; the default pool of 10 would
        # discard and rebuild connections under a burst of merged reads.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def request(
        self,
        instance: Instance,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        data: bytes | None = None,
    ) -> requests.Response:
        url = instance.endpoint(path)
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                data=data,
                timeout=(self.connect_timeout, self.timeout),
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            raise UpstreamError(instance, exc) from exc

        elapsed = response.elapsed.total_seconds()
        if elapsed >= self.slow_after:
            logger.warning(
                "%s took %.1fs for %s %s (timeout is %.0fs). If this keeps climbing, "
                "raise UPSTREAM_TIMEOUT - a spun-down array or a large library can "
                "outlast the default.",
                instance.label,
                elapsed,
                method,
                redact(path),
                self.timeout,
            )
        return response

    def json(
        self,
        instance: Instance,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
    ) -> tuple[Any, int]:
        response = self.request(
            instance, method, path, headers=headers, params=params, json_data=json_data
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return payload, response.status_code

    def fetch(
        self,
        instance: Instance,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, int | None]:
        """GET that never raises.

        Returns ``(payload, answered, status)``; ``status`` is ``None`` when the
        instance could not be contacted at all.
        """
        try:
            payload, status = self.json(instance, "GET", path, headers=headers, params=params)
        except UpstreamError as exc:
            logger.warning("%s", exc.log_message)
            return None, False, None
        if status >= 400 or payload is None:
            logger.warning(
                "%s returned HTTP %s for %s: %s",
                instance.label,
                status,
                redact(path),
                error_detail_from(payload),
            )
            return None, False, status
        return payload, True, status

    def json_or_empty(
        self,
        instance: Instance,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        default: Any = None,
    ) -> Any:
        """``fetch`` for callers that treat an unreachable instance as empty."""
        payload, ok, _status = self.fetch(instance, path, headers=headers, params=params)
        if not ok:
            return [] if default is None else default
        return payload


def to_flask_response(response: requests.Response) -> Response:
    headers = [
        (key, value)
        for key, value in response.headers.items()
        if key.lower() in FORWARDED_RESPONSE_HEADERS
    ]
    return Response(response.content, status=response.status_code, headers=headers)

