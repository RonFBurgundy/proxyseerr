"""Translation between the proxy's merged ID space and each instance's own IDs.

Sonarr and Radarr hand out per-instance auto-increment IDs, so English series 42
and Anime series 42 both exist. Seerr only ever talks to one "server", so every
ID the proxy hands back for the anime instance is shifted by ``id_offset``. Any
ID Seerr sends back is decoded to ``(instance_key, real_id)``, which is both the
routing decision and the value forwarded upstream.
"""
from __future__ import annotations

from typing import Any, Iterable

from .config import ANIME, ENGLISH
from .kinds import MediaKind

PLAIN_ID_FIELDS = ("id",)


def encode_id(value: Any, offset: int) -> Any:
    """Shift a real upstream ID into the proxy's ID space."""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    if value <= 0:
        return value
    return value + offset


def decode_id(value: Any, offset: int) -> tuple[str, Any]:
    """Map a proxy-space ID back to ``(instance_key, real_id)``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return ENGLISH, value
    if value >= offset:
        return ANIME, value - offset
    return ENGLISH, value


def is_anime_id(value: Any, offset: int) -> bool:
    return decode_id(value, offset)[0] == ANIME


def _shift(obj: Any, fields: Iterable[str], list_fields: Iterable[str], delta) -> Any:
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for field in fields:
        if field in out:
            out[field] = delta(out[field])
    for field in list_fields:
        values = out.get(field)
        if isinstance(values, list):
            out[field] = [delta(v) for v in values]
    return out


def encode_obj(obj: Any, offset: int, fields=PLAIN_ID_FIELDS, list_fields=()) -> Any:
    if offset == 0:
        return obj
    return _shift(obj, fields, list_fields, lambda v: encode_id(v, offset))


def decode_obj(obj: Any, offset: int, fields=PLAIN_ID_FIELDS, list_fields=()) -> Any:
    return _shift(obj, fields, list_fields, lambda v: decode_id(v, offset)[1])


def encode_item(item: Any, kind: MediaKind, offset: int) -> Any:
    """Namespace a series or movie object."""
    return encode_obj(item, offset, kind.item_id_fields, kind.item_list_fields)


def decode_item(item: Any, kind: MediaKind, offset: int) -> Any:
    return decode_obj(item, offset, kind.item_id_fields, kind.item_list_fields)


def encode_list(items: Any, offset: int, fields=PLAIN_ID_FIELDS, list_fields=()) -> Any:
    if not isinstance(items, list):
        return items
    return [encode_obj(item, offset, fields, list_fields) for item in items]


def prefix_labels(items: Any, prefix: str, keys=("name",)) -> Any:
    """Tag anime-side dropdown entries so they are distinguishable in Seerr.

    Only display-only fields are touched. Root folder ``path`` values are left
    alone on purpose: Seerr echoes the path back verbatim in the add payload,
    so a decorated path would be forwarded upstream and rejected.
    """
    if not prefix or not isinstance(items, list):
        return items
    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        copy = dict(item)
        for key in keys:
            value = copy.get(key)
            if isinstance(value, str) and not value.startswith(prefix):
                copy[key] = f"{prefix}{value}"
                break
        out.append(copy)
    return out


def strip_prefix(value: Any, prefix: str) -> Any:
    if prefix and isinstance(value, str) and value.startswith(prefix):
        return value[len(prefix):]
    return value
