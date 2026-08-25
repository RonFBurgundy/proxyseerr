"""HTTP plumbing for talking to the Sonarr backends."""
from __future__ import annotations

import logging
from typing import Any

import requests
from flask import Response

from .config import Instance

logger = logging.getLogger(__name__)

# Connection-scoped headers must never be copied between the two hops, and the
# body-describing ones are recomputed because `requests` hands us decoded bytes.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
STRIP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "accept-encoding", "x-api-key"}
STRIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length", "content-encoding"}


class UpstreamError(RuntimeError):
    def __init__(self, instance: Instance, exc: Exception):
        super().__init__(f"{instance.label} Sonarr unreachable at {instance.url}: {exc}")
        self.instance = instance
        self.original = exc


def build_headers(incoming: Any, instance: Instance) -> dict[str, str]:
    headers = {
        key: value
        for key, value in (incoming or {})
        if key.lower() not in STRIP_REQUEST_HEADERS
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

    def json_or_empty(
        self,
        instance: Instance,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        default: Any = None,
    ) -> Any:
        """Best-effort GET used by the merge endpoints; never raises."""
        fallback = [] if default is None else default
        try:
            payload, status = self.json(instance, "GET", path, headers=headers, params=params)
        except UpstreamError as exc:
            logger.warning("%s", exc)
            return fallback
        if status >= 400 or payload is None:
            logger.warning(
                "%s Sonarr returned HTTP %s for %s; treating as empty",
                instance.label,
                status,
                path,
            )
            return fallback
        return payload


def to_flask_response(response: requests.Response) -> Response:
    headers = [
        (key, value)
        for key, value in response.headers.items()
        if key.lower() not in STRIP_RESPONSE_HEADERS
    ]
    return Response(response.content, status=response.status_code, headers=headers)
