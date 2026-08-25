"""HTTP plumbing for talking to the Sonarr/Radarr backends."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from flask import Response

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

    def __init__(self, path: str):
        self.public_message = "No instance could be reached"
        self.log_message = f"Every instance failed for {redact(path)}; returning 502"
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


class Upstream:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self.session = requests.Session()

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
            return self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                data=data,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            raise UpstreamError(instance, exc) from exc

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
    ) -> tuple[Any, bool]:
        """GET that never raises, reporting whether the instance actually answered."""
        try:
            payload, status = self.json(instance, "GET", path, headers=headers, params=params)
        except UpstreamError as exc:
            logger.warning("%s", exc.log_message)
            return None, False
        if status >= 400 or payload is None:
            logger.warning(
                "%s returned HTTP %s for %s: %s",
                instance.label,
                status,
                redact(path),
                error_detail_from(payload),
            )
            return None, False
        return payload, True

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
        payload, ok = self.fetch(instance, path, headers=headers, params=params)
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

