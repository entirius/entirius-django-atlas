# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""advisory warnings for SourceMappingProfile validation.

Hard errors (Feature missing, channel missing) live in `mapping_service.validate_profile`.
This module adds *non-blocking* warnings the CMS surfaces as yellow badges:

1. `source_value_not_found_in_source_data` — operator typed a category source_value
   that doesn't appear in any SP for that source_field (likely typo).
2. `type_incompatibility` — too many SP values for an attribute mapping fail to parse
   as the target Feature's type (e.g. mapping `weight_g="350"` → BOOL feature).

Both checks rely on real SourceProduct.data (no fixtures, no guesses). They are best-effort:
if PIM is unimportable or the source has no SPs yet, checks are skipped silently — the
intent is to help operators catch real problems, not block them when data isn't ready.
"""

from __future__ import annotations

import difflib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from django_atlas.models import SourceAttributeMapping, SourceCategoryMapping, SourceMappingProfile, SourceProduct
from django_atlas.services import data_inspector_service

_TYPE_COMPAT_FAIL_PCT = 50  # >this many percent failed parses → warning
_TYPE_COMPAT_SAMPLE = 10  # how many SP values to sample for the parse check
_VALUES_LOOKUP_LIMIT = 10000  # enough headroom for known source scales (~4k SPs)
_BOOL_TRUE = frozenset({"true", "1", "yes", "y", "t", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "n", "f", "off"})


def collect_warnings(profile: SourceMappingProfile) -> list[dict[str, Any]]:
    """Return a list of MappingWarning-shaped dicts for the profile's mappings."""
    warnings: list[dict[str, Any]] = []
    source_idx = profile.source.idx

    cat_mappings = list(profile.category_mappings.all())
    attr_mappings = list(profile.attribute_mappings.all())

    warnings.extend(_collect_category_warnings(source_idx, cat_mappings))
    warnings.extend(_collect_attribute_warnings(source_idx, profile.source_id, attr_mappings))
    return warnings


# ---------------------------------------------------------------------------
# Category mapping — source_value presence check
# ---------------------------------------------------------------------------


def _collect_category_warnings(source_idx: str, mappings: list[SourceCategoryMapping]) -> list[dict[str, Any]]:
    if not mappings:
        return []
    # De-dupe lookups: many category mappings often share the same source_field.
    unique_source_fields = {m.source_field for m in mappings if m.source_field}
    values_by_field: dict[str, set[str]] = {}
    for source_field in unique_source_fields:
        try:
            payload = data_inspector_service.list_values(source_idx, source_field, limit=_VALUES_LOOKUP_LIMIT)
        except ValueError:
            continue  # source missing — outer validate already errors out
        values_by_field[source_field] = {v["value"] for v in payload["values"]}

    out: list[dict[str, Any]] = []
    for m in mappings:
        if not m.source_field or not m.source_value:
            continue
        known = values_by_field.get(m.source_field)
        if known is None or m.source_value in known:
            continue
        suggestion = _closest(m.source_value, known) if known else None
        out.append(
            {
                "code": "source_value_not_found_in_source_data",
                "message": (f"Source value '{m.source_value}' not found in any SourceProduct.{m.source_field}"),
                "mapping_kind": "category",
                "mapping_id": m.pk,
                "source_field": m.source_field,
                "source_value": m.source_value,
                "target_identifier": None,
                "details": {"sample_count": 0, "suggestion": suggestion},
            }
        )
    return out


def _closest(target: str, candidates: set[str]) -> str | None:
    """Suggest the closest typo-tolerant match. Capped to avoid pathological enums."""
    if not target or not candidates or len(candidates) > 500:
        return None
    matches = difflib.get_close_matches(target, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Attribute mapping — type compatibility check
# ---------------------------------------------------------------------------


def _collect_attribute_warnings(
    source_idx: str, source_id: int, mappings: list[SourceAttributeMapping]
) -> list[dict[str, Any]]:
    if not mappings:
        return []
    try:
        from django_pim.models.feature import Feature
        from django_pim.services import feature_service
    except ImportError:
        return []  # PIM not importable — skip silently (errors path also degrades gracefully)

    out: list[dict[str, Any]] = []
    for m in mappings:
        if m.target_type != "feature" or not m.source_field or not m.target_identifier:
            continue
        try:
            feature = feature_service.get_feature_by_idx(m.target_identifier)
        except Feature.DoesNotExist:
            continue  # hard error is already collected by mapping_service.validate_profile
        sample = _sample_values(source_id, m.source_field)
        if not sample:
            continue
        result = _check_type(feature, sample)
        if result is None:
            continue
        failed_count, expected_type = result
        failed_pct = round(failed_count / len(sample) * 100)
        if failed_pct <= _TYPE_COMPAT_FAIL_PCT:
            continue
        out.append(
            {
                "code": "type_incompatibility",
                "message": (f"{failed_pct}% of sampled values for '{m.source_field}' don't parse as {expected_type}"),
                "mapping_kind": "attribute",
                "mapping_id": m.pk,
                "source_field": m.source_field,
                "source_value": None,
                "target_identifier": m.target_identifier,
                "details": {
                    "sample_size": len(sample),
                    "failed_count": failed_count,
                    "failed_pct": failed_pct,
                    "expected_type": expected_type,
                },
            }
        )
    return out


def _sample_values(source_id: int, source_field: str) -> list[Any]:
    """Fetch up to _TYPE_COMPAT_SAMPLE SP values for the source_field via Python iteration.

    Cheaper than a separate SQL variant for small N. Dot-paths handled by walking the dict.
    """
    if "." in source_field:
        path = source_field.split(".")
        sps = SourceProduct.objects.filter(source_id=source_id).values_list("data", flat=True)[
            : _TYPE_COMPAT_SAMPLE * 5
        ]
        out: list[Any] = []
        for data in sps:
            value = _get_dot_path(data, path)
            if value is None:
                continue
            out.append(value)
            if len(out) >= _TYPE_COMPAT_SAMPLE:
                break
        return out
    qs = SourceProduct.objects.filter(source_id=source_id, data__has_key=source_field).values_list("data", flat=True)[
        :_TYPE_COMPAT_SAMPLE
    ]
    out = []
    for data in qs:
        if data is None:
            continue
        value = data.get(source_field)
        if value is None:
            continue
        out.append(value)
    return out


def _get_dot_path(data: Any, path: list[str]) -> Any:
    cur = data
    for segment in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(segment)
        if cur is None:
            return None
    return cur


def _check_type(feature: Any, sample: list[Any]) -> tuple[int, str] | None:
    """Return (failed_count, expected_type_label) or None when the type is auto-OK."""
    from django_pim.models.feature import FeatureTypeEnum

    ftype = feature.feature_type
    label = _type_label(ftype)
    if ftype in {
        FeatureTypeEnum.UNKNOWN,
        FeatureTypeEnum.VARCHAR255,
        FeatureTypeEnum.VARCHAR255_T9N,
        FeatureTypeEnum.TEXT,
        FeatureTypeEnum.TEXT_T9N,
    }:
        return None  # always parseable as text — never warn

    failed = sum(1 for v in sample if not _parses(ftype, v, feature))
    return failed, label


def _parses(ftype: Any, value: Any, feature: Any) -> bool:
    from django_pim.models.feature import FeatureTypeEnum

    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    text = str(value).strip()
    if ftype == FeatureTypeEnum.BOOL:
        return text.lower() in _BOOL_TRUE | _BOOL_FALSE
    if ftype in {FeatureTypeEnum.DECIMAL, FeatureTypeEnum.TEMPERATURE, FeatureTypeEnum.LENGTH, FeatureTypeEnum.MASS}:
        try:
            Decimal(text.replace(",", "."))
            return True
        except InvalidOperation:
            return False
    if ftype in {FeatureTypeEnum.SELECT, FeatureTypeEnum.MULTISELECT}:
        try:
            from django_pim.models.attribute import Attribute
        except ImportError:
            return True  # PIM degraded — don't warn
        allowed = set(Attribute.objects.filter(feature=feature).values_list("idx", flat=True))
        if not allowed:
            return True  # no choices defined — can't tell
        if ftype == FeatureTypeEnum.MULTISELECT:
            parts = [p.strip() for p in text.split(",") if p.strip()]
            return all(p in allowed for p in parts) if parts else False
        return text in allowed
    if ftype in {FeatureTypeEnum.JSON, FeatureTypeEnum.JSON_T9N}:
        if isinstance(value, dict | list):
            return True
        try:
            json.loads(text)
            return True
        except (json.JSONDecodeError, TypeError):
            return False
    if ftype == FeatureTypeEnum.DATETIME:
        from datetime import datetime

        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return True  # unknown enum — be conservative


def _type_label(ftype: Any) -> str:
    from django_pim.models.feature import FeatureTypeEnum

    return {
        FeatureTypeEnum.BOOL: "bool",
        FeatureTypeEnum.DECIMAL: "decimal",
        FeatureTypeEnum.VARCHAR255: "text",
        FeatureTypeEnum.VARCHAR255_T9N: "text",
        FeatureTypeEnum.TEXT: "text",
        FeatureTypeEnum.TEXT_T9N: "text",
        FeatureTypeEnum.SELECT: "select option",
        FeatureTypeEnum.MULTISELECT: "select options",
        FeatureTypeEnum.JSON: "json",
        FeatureTypeEnum.JSON_T9N: "json",
        FeatureTypeEnum.DATETIME: "datetime",
        FeatureTypeEnum.TEMPERATURE: "decimal (temperature)",
        FeatureTypeEnum.LENGTH: "decimal (length)",
        FeatureTypeEnum.MASS: "decimal (mass)",
    }.get(ftype, "unknown")
