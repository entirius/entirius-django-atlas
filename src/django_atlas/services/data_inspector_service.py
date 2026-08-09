# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inspect SourceProduct.data JSON for combobox source-of-truth.

Flattens nested dicts to dot-paths so the CMS mapping form can offer operator
a real combobox of source feed keys instead of a free-text input (`list_keys`).
adds `list_values`: distinct source values per `source_field` with counts
for the source_value picker in CategoryMapping rows. Both endpoints share one cache
namespace per source, invalidated together by `execute_feed_task` on success.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.db import connection

from django_atlas.models import SourceProduct
from django_atlas.services import source_service

CACHE_TTL_SECONDS = 300
CACHE_KEY_PREFIX = "source"
CACHE_KEY_SUFFIX = "data-keys"
CACHE_KEY_SUFFIX_VALUES = "data-values"
CACHE_KEY_SUFFIX_VALUES_REGISTRY = "data-values-keys"
CACHE_TTL_REGISTRY_SECONDS = CACHE_TTL_SECONDS * 2  # outlive entries so cleanup finds them

# Reserved tokens: always offered, never derived from .data
_TOKENS: list[dict[str, str]] = [
    {"key": "__name__", "description": "Mapped from SourceProduct.name"},
    {"key": "__cost__", "description": "Mapped from SourceProduct.cost"},
    {"key": "__ean__", "description": "Mapped from SourceProduct.ean"},
]


def cache_key(source_idx: str) -> str:
    return f"{CACHE_KEY_PREFIX}:{source_idx}:{CACHE_KEY_SUFFIX}"


def cache_key_values(source_idx: str, source_field: str) -> str:
    """Per-(source, source_field) cache slot. SHA1 keeps key length bounded for arbitrary dot-paths."""
    digest = hashlib.sha1(source_field.encode("utf-8")).hexdigest()[:10]  # noqa: S324 — cache key only, not crypto
    return f"{CACHE_KEY_PREFIX}:{source_idx}:{CACHE_KEY_SUFFIX_VALUES}:{digest}"


def cache_key_values_registry(source_idx: str) -> str:
    """Registry set of every source_field ever cached for this source. Drives invalidate()."""
    return f"{CACHE_KEY_PREFIX}:{source_idx}:{CACHE_KEY_SUFFIX_VALUES_REGISTRY}"


def invalidate(source_idx: str) -> None:
    """Drop both data-keys and every data-values slot for this source.

    Registry-set pattern works on LocMemCache and Redis equally (no delete_pattern needed).
    """
    cache.delete(cache_key(source_idx))
    registry_key = cache_key_values_registry(source_idx)
    for source_field in cache.get(registry_key) or []:
        cache.delete(cache_key_values(source_idx, source_field))
    cache.delete(registry_key)


@dataclass(frozen=True)
class _Aggregate:
    count: int
    sample_value: str
    type_name: str


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _flatten(payload: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Walk nested dicts producing (dot_path, leaf_value) pairs.

    Arrays are treated as scalar leaves (no element expansion) by design.
    Non-dict top-level payloads yield nothing.
    """
    if not isinstance(payload, dict):
        return
    for raw_key, value in payload.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, prefix=path)
        else:
            yield path, value


def list_keys(source_idx: str, *, sample_size: int = 100) -> dict[str, Any]:
    """Return tokens + flattened data keys with presence_pct, sample, type.

    Raises ValueError if source does not exist.
    """
    if sample_size < 1:
        raise ValueError("sample_size must be >= 1")

    cached = cache.get(cache_key(source_idx))
    if cached is not None:
        return cached

    source = source_service.get_source(source_idx)

    rows = list(SourceProduct.objects.filter(source_id=source.id).values_list("data", flat=True)[:sample_size])
    actual_size = len(rows)

    aggregates: dict[str, _Aggregate] = {}
    for row in rows:
        seen_in_row: set[str] = set()
        for path, value in _flatten(row or {}):
            if path in seen_in_row:
                continue
            seen_in_row.add(path)
            prior = aggregates.get(path)
            if prior is None:
                aggregates[path] = _Aggregate(count=1, sample_value=_stringify(value), type_name=_value_type(value))
            else:
                aggregates[path] = _Aggregate(
                    count=prior.count + 1, sample_value=prior.sample_value, type_name=prior.type_name
                )

    data_keys: list[dict[str, Any]] = []
    for path, agg in aggregates.items():
        presence_pct = round(agg.count / actual_size * 100) if actual_size else 0
        data_keys.append(
            {"key": path, "presence_pct": presence_pct, "sample_value": agg.sample_value, "type": agg.type_name}
        )
    data_keys.sort(key=lambda item: (-item["presence_pct"], item["key"]))

    payload = {"tokens": [dict(token) for token in _TOKENS], "data_keys": data_keys, "sample_size": actual_size}
    cache.set(cache_key(source_idx), payload, CACHE_TTL_SECONDS)
    return payload


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return str(value)[:120]


def list_values(source_idx: str, source_field: str, *, limit: int = 500) -> dict[str, Any]:
    """Return distinct values + counts for `source_field` across the source's SourceProducts.

    Postgres-side GROUP BY (true cardinality, not sample) so the operator sees real
    frequencies in the picker. Flat key uses `data ->> %s`; dot-path uses `data #>> %s`
    with the path split into a text[]. Truncates to `limit`; sets `truncated=True` and
    runs a separate COUNT(DISTINCT) only when the result was actually trimmed.

    Raises ValueError on missing source or invalid arguments.
    """
    if not source_field:
        raise ValueError("source_field is required")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    cached = cache.get(cache_key_values(source_idx, source_field))
    if cached is not None:
        return cached

    source = source_service.get_source(source_idx)

    rows = _query_values(source.id, source_field, limit + 1)
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]

    values = [{"value": _stringify_db_value(v), "count": int(c)} for v, c in rows]
    total_distinct = _count_distinct(source.id, source_field) if truncated else None

    payload: dict[str, Any] = {
        "source_field": source_field,
        "values": values,
        "total_distinct": total_distinct,
        "truncated": truncated,
        "sample_scope": "all",
    }

    cache.set(cache_key_values(source_idx, source_field), payload, CACHE_TTL_SECONDS)
    _register_value_field(source_idx, source_field)
    return payload


def _register_value_field(source_idx: str, source_field: str) -> None:
    """Track every source_field cached for this source so invalidate() can find them."""
    registry_key = cache_key_values_registry(source_idx)
    fields = list(cache.get(registry_key) or [])
    if source_field not in fields:
        fields.append(source_field)
        cache.set(registry_key, fields, CACHE_TTL_REGISTRY_SECONDS)


def _query_values(source_id: int, source_field: str, fetch_limit: int) -> list[tuple[Any, int]]:
    # `table` is sourced from SourceProduct._meta.db_table (compile-time constant) —
    # not user input. All user values are passed as bind parameters. S608 is a false positive.
    table = SourceProduct._meta.db_table
    if "." in source_field:
        path = source_field.split(".")
        sql = (
            f'SELECT data #>> %s AS v, COUNT(*) AS c FROM "{table}" '  # noqa: S608 — table from ORM meta, values bound
            "WHERE source_id = %s AND data #>> %s IS NOT NULL "
            "GROUP BY v ORDER BY c DESC, v ASC LIMIT %s"
        )
        params: list[Any] = [path, source_id, path, fetch_limit]
    else:
        sql = (
            f'SELECT data ->> %s AS v, COUNT(*) AS c FROM "{table}" '  # noqa: S608 — table from ORM meta, values bound
            "WHERE source_id = %s AND data ? %s AND data ->> %s IS NOT NULL "
            "GROUP BY v ORDER BY c DESC, v ASC LIMIT %s"
        )
        params = [source_field, source_id, source_field, source_field, fetch_limit]
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _count_distinct(source_id: int, source_field: str) -> int:
    table = SourceProduct._meta.db_table
    if "." in source_field:
        path = source_field.split(".")
        sql = (
            f'SELECT COUNT(DISTINCT data #>> %s) FROM "{table}" '  # noqa: S608 — table from ORM meta, values bound
            "WHERE source_id = %s AND data #>> %s IS NOT NULL"
        )
        params: list[Any] = [path, source_id, path]
    else:
        sql = (
            f'SELECT COUNT(DISTINCT data ->> %s) FROM "{table}" '  # noqa: S608 — table from ORM meta, values bound
            "WHERE source_id = %s AND data ? %s AND data ->> %s IS NOT NULL"
        )
        params = [source_field, source_id, source_field, source_field]
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _stringify_db_value(value: Any) -> str:
    """Normalize a Postgres ->> result. Always returns str, trimmed to 200 chars."""
    if value is None:
        return ""
    return str(value).strip()[:200]
