# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""PIM writer — bridge from SourceProduct to PIM RealProduct/Product/ProductAttribute/etc.

Implements stage-4 push end-to-end:

- generate_sku  — deterministic SKU per (source, external_id)
- resolve_*     — feature_set / language hierarchy
- apply_*       — attribute / real_product / category mappings
- init_push_to_channel  — INIT push (per channel)
- force_repush_to_channel — overwrite path

Frozen decisions in scope:
  disjoint target_channel_idxs across active profiles (mapping_service handles save-time;
        push uses idempotent skip on pushed_to_channel_idxs as layer 2).
  MULTISELECT mapping iterates per element; partial missing = warning + skip element.
  ProductAttribute.objects.create() per row (NEVER bulk_create) — PIM relies on post_save
        signals (cache invalidation, search reindex, inheritance materialization).
  PIM product_picture_service.upload_picture must be idempotent (sha1 dedup). By design
        delegates picture creation to image_download_task; upload_picture handles dedup.
"""

import hashlib
import logging
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType
from django_atlas.models import (
    AttributeMappingTargetType,
    Source,
    SourceAttributeMapping,
    SourceMappingProfile,
    SourceProduct,
)
from django_atlas.services import audit_service, event_service, realproduct_match_service, value_transformer
from django_atlas.services.realproduct_match_service import ToleranceResult
from django_atlas.services.value_transformer import TransformResult
from django_atlas.validators import validate_routable_sku

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------

# Sentinels for `source`; kept as strings (no enum) since they leak into JSON audit rows.
LANG_SOURCE_PROFILE_OVERRIDE = "profile_override"
LANG_SOURCE_SOURCE_DEFAULT = "source_default"
LANG_SOURCE_CHANNEL_DEFAULT_FALLBACK = "channel_default_fallback"


@dataclass(frozen=True)
class LanguageResolution:
    """Resolution outcome for the per-channel target language during push.

    Hierarchy (in resolve_language_for_channel):
      1. profile.import_language → source=profile_override
      2. source.default_language ∈ channel.languages → source=source_default
      3. otherwise → source=channel_default_fallback (used_fallback=True)
    """

    language: str
    source: str
    used_fallback: bool
    source_language: str
    channel_languages: list[str] = dc_field(default_factory=list)
    channel_default_language: str = ""


def _json_safe(value: Any) -> Any:
    """Decimal → str for JSONField stability."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _flush_audit_drafts(drafts: list[dict[str, Any]], *, context: str) -> None:
    """Out-of-band: audit log failure NEVER crashes the push path."""
    if not drafts:
        return
    try:
        audit_service.log_changes_bulk(drafts)
    except Exception:  # noqa: BLE001 — audit must be best-effort
        logger.warning("audit_service.log_changes_bulk failed in %s", context, exc_info=True)


def _sp_baseline_snapshot(sp: SourceProduct) -> dict[str, Any]:
    """Capture SP state for init_push baseline audit entry."""
    return _json_safe(
        {
            "name": sp.name,
            "cost": sp.cost,
            "currency": sp.currency,
            "stock": sp.stock,
            "ean": sp.ean,
            "image_urls": list(sp.image_urls or []),
            "data": dict(sp.data or {}),
        }
    )


# Tokens for special source_field values (resolved before sp.data lookup)
_TOKEN_NAME = "__name__"  # noqa: S105 — token marker, not a credential
_TOKEN_COST = "__cost__"  # noqa: S105 — token marker, not a credential
_TOKEN_EAN = "__ean__"  # noqa: S105 — token marker, not a credential

_REAL_PRODUCT_FIELDS = frozenset({"weight", "ean", "width", "height", "deep", "kind_of_product"})
_REAL_PRODUCT_DECIMAL_FIELDS = frozenset({"weight", "width", "height", "deep"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_sku(source: Source, external_id: str) -> str:
    """Deterministic SKU: '{prefix}-{sha1(external_id)[:12]}' (or no prefix if blank).

    SHA1 used for non-cryptographic deduplication (deterministic mapping external_id → SKU).
    Validated against `validators.validate_routable_sku` — atlas is one of the few non-PIM
    writers of `RealProduct.sku`, so a generated SKU must not collide with a reserved
    `<path:sku>` sub-route word (raises ValueError, surfaced as 400 by the push view).
    """
    digest = hashlib.sha1(external_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    sku = f"{source.sku_prefix}-{digest}" if source.sku_prefix else digest
    validate_routable_sku(sku)
    return sku


def resolve_feature_set(sp: SourceProduct, feed, profile: SourceMappingProfile, source: Source) -> str:
    """First non-NULL wins: SP override → Feed → Profile → Source default."""
    candidates = [
        sp.feature_set_idx_override,
        feed.feature_set_idx if feed else None,
        profile.feature_set_idx,
        source.default_feature_set_idx,
    ]
    for value in candidates:
        if value:
            return value
    raise ValueError(f"no feature_set resolved for SP {sp.id}")


def resolve_language(profile: SourceMappingProfile, source: Source) -> str:
    """Profile import_language overrides source default_language.

    Legacy two-arg form. Prefer `resolve_language_for_channel` when a channel
    is available — it adds the intersect / channel-default fallback step
    required for correct multilingual attribute writes.
    """
    if profile.import_language is not None:
        return profile.import_language.iso2
    return source.default_language.iso2


def resolve_language_for_channel(profile: SourceMappingProfile, source: Source, channel) -> LanguageResolution:
    """Resolve the target language for a (profile, source, channel) tuple.

    Returns LanguageResolution so callers can decide whether to emit a fallback
    warning event + audit row.
    """
    source_lang = source.default_language.iso2
    channel_default = channel.default_language.iso2
    channel_langs = list(channel.languages.values_list("iso2", flat=True))

    if profile.import_language is not None:
        return LanguageResolution(
            language=profile.import_language.iso2,
            source=LANG_SOURCE_PROFILE_OVERRIDE,
            used_fallback=False,
            source_language=source_lang,
            channel_languages=channel_langs,
            channel_default_language=channel_default,
        )
    if source_lang in channel_langs:
        return LanguageResolution(
            language=source_lang,
            source=LANG_SOURCE_SOURCE_DEFAULT,
            used_fallback=False,
            source_language=source_lang,
            channel_languages=channel_langs,
            channel_default_language=channel_default,
        )
    return LanguageResolution(
        language=channel_default,
        source=LANG_SOURCE_CHANNEL_DEFAULT_FALLBACK,
        used_fallback=True,
        source_language=source_lang,
        channel_languages=channel_langs,
        channel_default_language=channel_default,
    )


def _emit_language_fallback(
    sp: SourceProduct,
    source: Source,
    profile: SourceMappingProfile,
    channel,
    resolution: LanguageResolution,
    user: AbstractBaseUser | None,
    *,
    context: str,
    event_sink: list[dict[str, Any]] | None,
    real_product_sku: str | None = None,
) -> None:
    """Side effects when a push falls back to channel.default_language.

    Writes IntegrationEvent + audit log row (best-effort, never blocks push)
    and, when caller provided an `event_sink`, appends a lightweight dict that
    the HTTP layer surfaces back to the CMS for toast notifications.
    """
    message = (
        f"Language fallback: source '{source.idx}' default_language='{resolution.source_language}' "
        f"not in channel '{channel.idx}' languages={sorted(resolution.channel_languages)}; "
        f"wrote translations to channel default '{resolution.channel_default_language}'. "
        "Set Profile.import_language to override."
    )
    details: dict[str, Any] = {
        "profile_idx": profile.idx,
        "channel_idx": channel.idx,
        "source_language": resolution.source_language,
        "channel_languages": resolution.channel_languages,
        "channel_default_language": resolution.channel_default_language,
        "resolved_language": resolution.language,
        "source": resolution.source,
    }
    try:
        event_service.record(
            event_type=EventType.LANGUAGE_FALLBACK.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=message,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log must be best-effort
        logger.warning("event_service.record failed in %s for language_fallback", context, exc_info=True)

    _flush_audit_drafts(
        [
            {
                "source_product": sp,
                "real_product_sku": real_product_sku,
                "source": ChangeLogSource.LANGUAGE_FALLBACK.value,
                "field_path": f"language_resolution.{channel.idx}",
                "before": None,
                "after": asdict(resolution),
                "triggered_by": user,
                "applied_to_pim": True,
            }
        ],
        context=context,
    )

    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.LANGUAGE_FALLBACK.value,
                "severity": EventSeverity.WARNING.value,
                "message": message,
                "details": details,
            }
        )


def _lookup_value(sp: SourceProduct, source_field: str) -> Any:
    """Resolve token tags or sp.data key."""
    if source_field == _TOKEN_NAME:
        return sp.name
    if source_field == _TOKEN_COST:
        return sp.cost
    if source_field == _TOKEN_EAN:
        return sp.ean or None
    return sp.data.get(source_field)


# ---------------------------------------------------------------------------
# Attribute application
# ---------------------------------------------------------------------------


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _create_select_attribute(product, feature, value: str, sp: SourceProduct, target_identifier: str) -> bool:
    """Single-value SELECT lookup. Returns True on success, False if attribute missing (event recorded)."""
    from django_pim.models.attribute import Attribute
    from django_pim.models.product_attribute import ProductAttribute

    try:
        attribute = Attribute.objects.get(feature=feature, idx=value)
    except Attribute.DoesNotExist:
        event_service.record(
            event_type=EventType.PIM_ATTRIBUTE_MISSING.value,
            severity=EventSeverity.WARNING.value,
            source_product=sp,
            message=f"Attribute '{value}' for feature '{target_identifier}' not found",
        )
        return False
    ProductAttribute.objects.create(product=product, feature=feature, attribute=attribute)
    return True


def _create_multiselect_attributes(product, feature, value, sp: SourceProduct, target_identifier: str) -> int:
    """MULTISELECT : iterate per element, skip missing with warning, return count created.

    Per-element create()  — bulk_create would skip post_save signals.
    """
    from django_pim.models.attribute import Attribute
    from django_pim.models.product_attribute import ProductAttribute

    if isinstance(value, str):
        elements = [value]
    elif isinstance(value, (list, tuple)):
        elements = list(value)
    else:
        event_service.record(
            event_type=EventType.PIM_ATTRIBUTE_MISSING.value,
            severity=EventSeverity.WARNING.value,
            source_product=sp,
            message=f"MULTISELECT feature '{target_identifier}' got non-list value: {value!r}",
        )
        return 0

    created = 0
    for element in elements:
        try:
            attribute = Attribute.objects.get(feature=feature, idx=element)
        except Attribute.DoesNotExist:
            event_service.record(
                event_type=EventType.PIM_ATTRIBUTE_MISSING.value,
                severity=EventSeverity.WARNING.value,
                source_product=sp,
                message=(f"Attribute '{element}' for MULTISELECT feature '{target_identifier}' not found, skipped"),
            )
            continue
        ProductAttribute.objects.create(product=product, feature=feature, attribute=attribute)
        created += 1
    return created


def _create_typed_attribute(product, feature, feature_type, value, language: str) -> None:
    """Create ProductAttribute for a non-SELECT FeatureType. per-row create()."""
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    kwargs: dict[str, Any] = {"product": product, "feature": feature}

    if feature_type == FeatureTypeEnum.BOOL:
        kwargs["value_bool"] = bool(value)
    elif feature_type in (
        FeatureTypeEnum.DECIMAL,
        FeatureTypeEnum.TEMPERATURE,
        FeatureTypeEnum.LENGTH,
        FeatureTypeEnum.MASS,
    ):
        decimal_value = _coerce_decimal(value)
        if decimal_value is None:
            return
        kwargs["value_decimal"] = decimal_value
    elif feature_type in (FeatureTypeEnum.VARCHAR255, FeatureTypeEnum.TEXT):
        kwargs["value_txt"] = str(value)
    elif feature_type in (FeatureTypeEnum.VARCHAR255_T9N, FeatureTypeEnum.TEXT_T9N):
        # Single-language store: {language: value}. Operators with raw multi-language
        # input must use a Phase 2 transformer or skip target_type='skip'.
        kwargs["value_txt_t9n"] = {language: str(value)}
    elif feature_type == FeatureTypeEnum.JSON:
        kwargs["value_json"] = value
    elif feature_type == FeatureTypeEnum.JSON_T9N:
        # PIM stores JSON_T9N in value_json keyed by language (see ProductAttribute.get_value).
        kwargs["value_json"] = {language: value}
    elif feature_type == FeatureTypeEnum.DATETIME:
        kwargs["value_datetime"] = value
    else:
        # UNKNOWN or unsupported — skip silently (catch-all).
        return

    ProductAttribute.objects.create(**kwargs)


def _record_transform_outcome(
    sp: SourceProduct, mapping: SourceAttributeMapping, raw_value: Any, result: TransformResult
) -> None:
    """Best-effort observability for modifier outcomes.

    On failure (type mismatch / invalid decimal / unknown modifier), emit a warning
    IntegrationEvent so the operator sees it in the events panel. Audit rows are
    only emitted when the transformed value is actually persisted — handled by
    callers because they alone know if a field changed."""
    if not result.failure_reason:
        return
    try:
        event_service.record(
            event_type=EventType.MAPPING_TRANSFORM_FAILED.value,
            severity=EventSeverity.WARNING.value,
            source_product=sp,
            message=(
                f"Mapping transform '{mapping.modifier}' failed on "
                f"'{mapping.source_field}' ({result.failure_reason}); raw value kept."
            ),
            details={
                "modifier": mapping.modifier,
                "source_field": mapping.source_field,
                "target_type": mapping.target_type,
                "target_identifier": mapping.target_identifier,
                "failure_reason": result.failure_reason,
                "raw_value": str(raw_value),
            },
        )
    except Exception:  # noqa: BLE001 — audit/event writes never block the push pipeline
        logger.exception("event_service.record failed for mapping_transform_failed (sp=%s)", sp.id)


def apply_attribute_mappings(product, sp: SourceProduct, profile: SourceMappingProfile, language: str) -> None:
    """Apply all feature target_type='feature' mappings to a Product. Real_product mappings handled separately."""
    from django_pim.models.feature import Feature, FeatureTypeEnum

    mappings = list(profile.attribute_mappings.all())
    for mapping in mappings:
        if mapping.target_type == AttributeMappingTargetType.SKIP.value:
            continue
        if mapping.target_type == AttributeMappingTargetType.REAL_PRODUCT.value:
            continue  # handled in apply_real_product_mappings

        raw_value = _lookup_value(sp, mapping.source_field)
        transform_result = value_transformer.transform(raw_value, mapping.modifier)
        _record_transform_outcome(sp, mapping, raw_value, transform_result)
        value = transform_result.value

        if value is None:
            if mapping.is_required:
                raise ValueError(f"required field '{mapping.source_field}' missing for SP {sp.id}")
            event_service.record(
                event_type=EventType.ATTRIBUTE_VALUE_MISSING.value,
                severity=EventSeverity.INFO.value,
                source_product=sp,
                message=f"Optional attribute value missing: '{mapping.source_field}'",
            )
            continue

        try:
            feature = Feature.objects.get(idx=mapping.target_identifier)
        except Feature.DoesNotExist:
            event_service.record(
                event_type=EventType.PIM_FEATURE_MISSING.value,
                severity=EventSeverity.WARNING.value,
                source_product=sp,
                message=f"PIM feature '{mapping.target_identifier}' not found",
            )
            continue

        feature_type = feature.feature_type

        if feature_type == FeatureTypeEnum.SELECT:
            if isinstance(value, (list, tuple)):
                event_service.record(
                    event_type=EventType.PIM_ATTRIBUTE_MISSING.value,
                    severity=EventSeverity.WARNING.value,
                    source_product=sp,
                    message=(f"SELECT feature '{mapping.target_identifier}' got list value {value!r}, expected str"),
                )
                continue
            _create_select_attribute(product, feature, str(value), sp, mapping.target_identifier)
        elif feature_type == FeatureTypeEnum.MULTISELECT:
            _create_multiselect_attributes(product, feature, value, sp, mapping.target_identifier)
        else:
            _create_typed_attribute(product, feature, feature_type, value, language)

        if transform_result.applied:
            _log_transform_audit(sp, "feature", mapping.target_identifier, raw_value, value)


def _log_transform_audit(sp: SourceProduct, target_kind: str, target_identifier: str, before: Any, after: Any) -> None:
    """Emit one MAPPING_TRANSFORM audit row. Best-effort: failure does not block push."""
    try:
        audit_service.log_change(
            source_product=sp,
            source=ChangeLogSource.MAPPING_TRANSFORM.value,
            field_path=f"{target_kind}.{target_identifier}",
            before=before,
            after=after,
        )
    except Exception:  # noqa: BLE001 — audit writes never block the push pipeline
        logger.exception("audit_service.log_change failed for mapping_transform (sp=%s)", sp.id)


def apply_real_product_mappings(
    real_product, sp: SourceProduct, profile: SourceMappingProfile, *, force_overwrite: bool = False
) -> bool:
    """Apply target_type='real_product' mappings. Returns True if changed.

    INIT (force_overwrite=False): skip when current value already set (preserve existing PIM data).
    Force re-push (force_overwrite=True): always overwrite.
    """
    mappings = profile.attribute_mappings.filter(target_type=AttributeMappingTargetType.REAL_PRODUCT.value)
    changed_fields: list[str] = []
    pending_transform_audits: list[tuple[str, Any, Any]] = []

    for mapping in mappings:
        field = mapping.target_identifier
        if field not in _REAL_PRODUCT_FIELDS:
            continue
        raw_value = _lookup_value(sp, mapping.source_field)
        if raw_value is None:
            continue

        transform_result = value_transformer.transform(raw_value, mapping.modifier)
        _record_transform_outcome(sp, mapping, raw_value, transform_result)
        value = transform_result.value

        if field in _REAL_PRODUCT_DECIMAL_FIELDS:
            value = _coerce_decimal(value)
            if value is None:
                continue

        current = getattr(real_product, field)
        if not force_overwrite and current not in (None, ""):
            continue
        if current == value:
            continue
        setattr(real_product, field, value)
        changed_fields.append(field)
        if transform_result.applied:
            pending_transform_audits.append((field, raw_value, value))

    if changed_fields:
        # ignore_validate_ean — Source-provided EAN may not be canonical; PIM legacy validator is strict.
        if "ean" in changed_fields:
            real_product.save(update_fields=changed_fields, ignore_validate_ean=True)
        else:
            real_product.save(update_fields=changed_fields)
        for field, before, after in pending_transform_audits:
            _log_transform_audit(sp, "real_product", field, before, after)
        return True
    return False


def apply_category_mappings(product, sp: SourceProduct, profile: SourceMappingProfile, channel_idx: str) -> None:
    """Match `sp.data[source_field] == source_value`, add ProductInCategory for each match in this channel."""
    from django_pim.models.product_category import ProductCategory
    from django_pim.models.product_in_category import ProductInCategory

    mappings = list(profile.category_mappings.all())
    for mapping in mappings:
        sp_value = sp.data.get(mapping.source_field)
        if sp_value != mapping.source_value:
            continue
        try:
            category = ProductCategory.objects.get(shop__idx=channel_idx, idx=mapping.target_category_idx)
        except ProductCategory.DoesNotExist:
            event_service.record(
                event_type=EventType.PIM_CATEGORY_MISSING.value,
                severity=EventSeverity.WARNING.value,
                source_product=sp,
                message=(f"PIM category '{mapping.target_category_idx}' not found in channel '{channel_idx}'"),
            )
            continue
        ProductInCategory.objects.get_or_create(product=product, category=category)


# ---------------------------------------------------------------------------
# Multi-source overlap detection
# ---------------------------------------------------------------------------


def _detect_multi_source_overlap(sp: SourceProduct, source: Source, sku: str) -> None:
    """Emit info event if SKU already linked to another source (shared PIM RealProduct)."""
    from django.apps import apps

    try:
        SourceProductLink = apps.get_model("django_atlas", "SourceProductLink")
    except LookupError:
        return  # SourceProductLink lands in stage 5 — no-op for stage 4 only.

    existing_links = SourceProductLink.objects.filter(real_product_sku=sku).exclude(source=source)
    other_idxs = [link.source.idx for link in existing_links.select_related("source")]
    if not other_idxs:
        return
    event_service.record(
        event_type=EventType.MULTI_SOURCE_OVERLAP.value,
        severity=EventSeverity.INFO.value,
        source_product=sp,
        message=f"SKU '{sku}' already linked to source(s) {other_idxs}",
        details={"existing_source_idxs": other_idxs, "new_source_idx": source.idx, "sku": sku},
    )


# ---------------------------------------------------------------------------
# Auto EAN-match
# ---------------------------------------------------------------------------


def _existing_source_idxs_for_sku(sku: str, exclude_source: Source) -> list[str]:
    """Helper for diagnostic event details — list of other source idxs linked to a SKU."""
    from django.apps import apps

    try:
        SourceProductLink = apps.get_model("django_atlas", "SourceProductLink")
    except LookupError:
        return []
    return list(
        SourceProductLink.objects.filter(real_product_sku=sku)
        .exclude(source=exclude_source)
        .select_related("source")
        .values_list("source__idx", flat=True)
    )


def _emit_auto_link_event(
    sp: SourceProduct, source: Source, matched_rp, tolerance: ToleranceResult, event_sink: list[dict[str, Any]] | None
) -> None:
    """Auto-linked an SP to an existing RealProduct via EAN. Severity INFO."""
    message = f"Auto-linked SP {sp.id} to RealProduct {matched_rp.sku} via EAN {sp.ean}"
    details = {
        "ean": sp.ean,
        "matched_sku": matched_rp.sku,
        "new_source_idx": source.idx,
        "existing_source_idxs": _existing_source_idxs_for_sku(matched_rp.sku, source),
        "diffs_pct": {k: str(v) for k, v in tolerance.diffs_pct.items()},
        "skipped_fields": list(tolerance.skipped_fields),
    }
    try:
        event_service.record(
            event_type=EventType.AUTO_LINKED_TO_EXISTING_REALPRODUCT.value,
            severity=EventSeverity.INFO.value,
            source=source,
            source_product=sp,
            message=message,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log must be best-effort
        logger.warning("event_service.record failed for auto_linked_to_existing_realproduct", exc_info=True)
    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.AUTO_LINKED_TO_EXISTING_REALPRODUCT.value,
                "severity": EventSeverity.INFO.value,
                "message": message,
                "details": details,
            }
        )


def _emit_tolerance_violation_event(
    sp: SourceProduct, source: Source, candidate_rp, tolerance: ToleranceResult, event_sink: list[dict[str, Any]] | None
) -> None:
    """EAN matched but physical diff > tolerance — falling back to new RealProduct. WARNING."""
    message = (
        f"Physical tolerance violation: SP {sp.id} EAN {sp.ean} matched RealProduct "
        f"{candidate_rp.sku} but {tolerance.failed_fields} exceeded "
        f"{source.realproduct_match_tolerance_pct}% — creating new RealProduct instead."
    )
    details = {
        "ean": sp.ean,
        "matched_sku": candidate_rp.sku,
        "new_source_idx": source.idx,
        "tolerance_pct": int(source.realproduct_match_tolerance_pct),
        "strict_mode": bool(source.realproduct_match_strict),
        "failed_fields": list(tolerance.failed_fields),
        "skipped_fields": list(tolerance.skipped_fields),
        "diffs_pct": {k: str(v) for k, v in tolerance.diffs_pct.items()},
    }
    try:
        event_service.record(
            event_type=EventType.PHYSICAL_TOLERANCE_VIOLATION.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=message,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log must be best-effort
        logger.warning("event_service.record failed for physical_tolerance_violation", exc_info=True)
    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.PHYSICAL_TOLERANCE_VIOLATION.value,
                "severity": EventSeverity.WARNING.value,
                "message": message,
                "details": details,
            }
        )


def _log_auto_link_audit(
    sp: SourceProduct, source: Source, matched_rp, tolerance: ToleranceResult, user: AbstractBaseUser | None
) -> None:
    """Audit row for the auto-link decision. Source=auto_link, field_path=real_product.link."""
    _flush_audit_drafts(
        [
            {
                "source_product": sp,
                "real_product_sku": matched_rp.sku,
                "source": ChangeLogSource.AUTO_LINK.value,
                "field_path": "real_product.link",
                "before": None,
                "after": {
                    "sku": matched_rp.sku,
                    "ean": sp.ean,
                    "matched_via": "ean",
                    "source_idx": source.idx,
                    "diffs_pct": {k: str(v) for k, v in tolerance.diffs_pct.items()},
                    "skipped_fields": list(tolerance.skipped_fields),
                },
                "triggered_by": user,
                "applied_to_pim": True,
            }
        ],
        context="init_push_auto_link",
    )


# ---------------------------------------------------------------------------
# INIT push + force re-push
# ---------------------------------------------------------------------------


def _get_channel(channel_idx: str):
    """Channel lookup with fallback (PIM has no Channel.get_by_idx cached method yet)."""
    from django_pim.models.channel import Channel

    return Channel.objects.get(idx=channel_idx)


def _get_feature_set(feature_set_idx: str):
    from django_pim.models.feature_set import FeatureSet

    try:
        return FeatureSet.objects.get(idx=feature_set_idx)
    except FeatureSet.DoesNotExist as exc:
        raise ValueError(f"PIM feature_set '{feature_set_idx}' not found") from exc


def _real_product_defaults(sp: SourceProduct, profile: SourceMappingProfile) -> dict[str, Any]:
    """Build defaults dict for RealProduct.objects.get_or_create from real_product mappings."""
    defaults: dict[str, Any] = {}
    for mapping in profile.attribute_mappings.filter(target_type=AttributeMappingTargetType.REAL_PRODUCT.value):
        field = mapping.target_identifier
        if field not in _REAL_PRODUCT_FIELDS:
            continue
        value = _lookup_value(sp, mapping.source_field)
        if value is None:
            continue
        if field in _REAL_PRODUCT_DECIMAL_FIELDS:
            value = _coerce_decimal(value)
            if value is None:
                continue
        defaults[field] = value
    if sp.ean and "ean" not in defaults:
        defaults["ean"] = sp.ean
    return defaults


def _persist_sp_after_push(sp: SourceProduct, real_product, channel_idx: str, user: AbstractBaseUser | None) -> None:
    """Update SP after a successful per-channel push. Status managed by push_service."""
    if sp.real_product_id is None:
        sp.real_product = real_product
    pushed = list(sp.pushed_to_channel_idxs or [])
    if channel_idx not in pushed:
        pushed.append(channel_idx)
        sp.pushed_to_channel_idxs = pushed
    sp.pushed_by = user
    sp.pushed_at = timezone.now()
    sp.save(update_fields=["real_product", "pushed_to_channel_idxs", "pushed_by", "pushed_at", "modified_at"])


def _emit_pre_linked_event(
    sp: SourceProduct, source: Source, real_product, event_sink: list[dict[str, Any]] | None
) -> None:
    """Push targeted the RealProduct the SP is already attached to (lookup UI / proposal). INFO."""
    message = f"SP {sp.id} pushed onto its linked RealProduct {real_product.sku} — no new RealProduct created"
    details = {
        "matched_sku": real_product.sku,
        "matched_via": "existing_link",
        "new_source_idx": source.idx,
        "existing_source_idxs": _existing_source_idxs_for_sku(real_product.sku, source),
    }
    try:
        event_service.record(
            event_type=EventType.AUTO_LINKED_TO_EXISTING_REALPRODUCT.value,
            severity=EventSeverity.INFO.value,
            source=source,
            source_product=sp,
            message=message,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log must be best-effort
        logger.warning("event_service.record failed for pre-linked push", exc_info=True)
    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.AUTO_LINKED_TO_EXISTING_REALPRODUCT.value,
                "severity": EventSeverity.INFO.value,
                "message": message,
                "details": details,
            }
        )


def _resolve_push_target(
    sp: SourceProduct,
    source: Source,
    profile: SourceMappingProfile,
    user: AbstractBaseUser | None,
    event_sink: list[dict[str, Any]] | None,
):
    """RealProduct this push writes to. An existing `sp.real_product` wins over every other rule.

    Anything that already attached the SP (lookup UI, enrichment proposal, an earlier push) decided
    the target; generating a per-source SKU here would spawn the duplicate that link exists to
    prevent. Only an unlinked SP walks the legacy path: EAN auto-match first, generated SKU after.
    """
    from django_pim.models.real_product import RealProduct

    if sp.real_product_id is not None:
        _emit_pre_linked_event(sp, source, sp.real_product, event_sink)
        return sp.real_product

    rp_defaults = _real_product_defaults(sp, profile)
    candidate_rp = realproduct_match_service.find_match_by_ean(sp, source)
    if candidate_rp is not None:
        tolerance = realproduct_match_service.physical_tolerance_check(rp_defaults, candidate_rp, source)
        if tolerance.passed:
            _emit_auto_link_event(sp, source, candidate_rp, tolerance, event_sink)
            _log_auto_link_audit(sp, source, candidate_rp, tolerance, user)
            return candidate_rp
        _emit_tolerance_violation_event(sp, source, candidate_rp, tolerance, event_sink)

    real_product, rp_created = RealProduct.objects.get_or_create(
        sku=generate_sku(source, sp.external_id), defaults=rp_defaults
    )
    if not rp_created:
        _detect_multi_source_overlap(sp, source, real_product.sku)
    return real_product


def _emit_pushed_signal(sp: SourceProduct, real_product_sku: str, channel_idx: str) -> None:
    from django_atlas.signals import is_suppressed, source_product_pushed_signal

    if is_suppressed():
        return
    source_product_pushed_signal.send(
        sender=type(sp), source_product=sp, real_product_sku=real_product_sku, channel_idx=channel_idx
    )


def init_push_to_channel(
    sp: SourceProduct,
    profile: SourceMappingProfile,
    channel_idx: str,
    user: AbstractBaseUser | None,
    *,
    event_sink: list[dict[str, Any]] | None = None,
):
    """INIT push: create RealProduct (if needed) + Product + ProductAttributes + ProductInCategory.

    NOT idempotent for `Product` — caller (push_service) must skip channels already pushed.
    Called inside push_service.transaction.atomic() block.

    `event_sink` (optional): when caller wants per-push warning events surfaced
    back through HTTP, pass a list — fresh `{event_type,
    severity, message, details}` dicts are appended for each warning fired
    during this call.
    """
    from django_pim.models.product import Product, ProductVisibilityEnum

    from django_atlas.services import kind_guard

    source = sp.source
    # Defense-in-depth: push_service.preflight_check already blocks monitoring/enrichment
    # before this is reached, but real_product FK writes are severe enough (PIM catalog
    # mutation) to re-assert the gate here rather than rely solely on the caller.
    kind_guard.assert_push_allowed(source, "init push to PIM channel")
    feature_set_idx = resolve_feature_set(sp, sp.feed, profile, source)

    channel = _get_channel(channel_idx)
    feature_set = _get_feature_set(feature_set_idx)

    resolution = resolve_language_for_channel(profile, source, channel)
    language = resolution.language

    real_product = _resolve_push_target(sp, source, profile, user, event_sink)

    product, p_created = Product.objects.get_or_create(
        real_product=real_product,
        shop=channel,
        defaults={
            "feature_set": feature_set,
            "is_enabled": False,
            "visibility": int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
            "product_class": source.default_product_class,
        },
    )
    if not p_created:
        raise ValueError(
            f"Product already exists for SKU {real_product.sku} on channel {channel_idx} — "
            f"use force_repush instead, or check for multi-source collision"
        )

    apply_attribute_mappings(product, sp, profile, language)
    apply_category_mappings(product, sp, profile, channel_idx)

    _persist_sp_after_push(sp, real_product, channel_idx, user)
    _write_qms_cost_link(sp, source, [channel_idx], event_sink=event_sink)
    _emit_pushed_signal(sp, real_product.sku, channel_idx)
    # Audit baseline — snapshot SP state at first push to channel; future diffs anchor here.
    _flush_audit_drafts(
        [
            {
                "source_product": sp,
                "real_product_sku": real_product.sku,
                "source": ChangeLogSource.INIT_PUSH.value,
                "field_path": f"snapshot.{channel_idx}",
                "before": None,
                "after": _sp_baseline_snapshot(sp),
                "triggered_by": user,
                "applied_to_pim": True,
            }
        ],
        context="init_push",
    )
    if resolution.used_fallback:
        _emit_language_fallback(
            sp,
            source,
            profile,
            channel,
            resolution,
            user,
            context="init_push",
            event_sink=event_sink,
            real_product_sku=real_product.sku,
        )
    return product


def _write_qms_cost_link(
    sp: SourceProduct, source: Source, channel_idxs: list[str], *, event_sink: list[dict[str, Any]] | None = None
) -> None:
    """Stage 5 integration: QMS stock + pricemanager cost log + SourceProductLink upsert.

    Imported lazily so stage-4-only environments (no SourceProductLink) keep working.

    after the link upsert, evaluate auto-primary selection on the
    RealProduct. When >1 active link exists, hysteresis + cooldown apply; when
    only one link exists, this is a no-op (set_primary_if_first handled it
    at creation).
    """
    from django_atlas.services import pricemanager_writer, primary_strategy_service, product_link_service, qms_writer

    qms_writer.write_stock(sp, source, channel_idxs, context="push")
    pricemanager_writer.log_cost(sp, source, channel_idxs, context="push")
    if sp.real_product_id is not None:
        # 1st-link-for-SKU becomes primary by default. Critical for the
        # cost subscriber which previously ignored single-source links where is_primary
        # defaulted to False.
        product_link_service.upsert_for_push(sp.real_product.sku, source, sp.external_id, set_primary_if_first=True)
        try:
            primary_strategy_service.maybe_trigger_inline_after_link(sp.real_product.sku, event_sink=event_sink)
        except Exception:  # noqa: BLE001 — primary eval must never break a push
            logger.warning(
                "primary_strategy_service.maybe_trigger_inline_after_link failed for sku=%s",
                sp.real_product.sku,
                exc_info=True,
            )


def force_repush_to_channel(
    sp: SourceProduct,
    profile: SourceMappingProfile,
    channel_idx: str,
    user: AbstractBaseUser | None,
    *,
    event_sink: list[dict[str, Any]] | None = None,
):
    """Overwrite path: delete + recreate attributes/categories/pictures, optionally overwrite physical.

    Falls back to init_push_to_channel if RealProduct/Product missing.
    Does NOT toggle Product.is_enabled (operator-controlled).

    `event_sink` (optional): same contract as init_push_to_channel — forwarded
    on recursive fallback into init_push_to_channel so events bubble up.
    """
    from django_pim.models.product import Product
    from django_pim.models.product_attribute import ProductAttribute
    from django_pim.models.product_in_category import ProductInCategory
    from django_pim.models.product_picture import ProductPicture
    from django_pim.models.real_product import RealProduct

    from django_atlas.services import kind_guard

    source = sp.source
    # Defense-in-depth — see init_push_to_channel.
    kind_guard.assert_push_allowed(source, "force re-push to PIM channel")
    channel = _get_channel(channel_idx)
    resolution = resolve_language_for_channel(profile, source, channel)
    language = resolution.language

    # Mirrors init_push_to_channel: a linked SP re-pushes onto ITS RealProduct, never onto the
    # per-source generated SKU (which would be a second row for the same product).
    if sp.real_product_id is not None:
        real_product = sp.real_product
    else:
        try:
            real_product = RealProduct.objects.get(sku=generate_sku(source, sp.external_id))
        except RealProduct.DoesNotExist:
            return init_push_to_channel(sp, profile, channel_idx, user, event_sink=event_sink)
    try:
        product = Product.objects.get(real_product=real_product, shop=channel)
    except Product.DoesNotExist:
        return init_push_to_channel(sp, profile, channel_idx, user, event_sink=event_sink)

    # --- Snapshot PIM state BEFORE delete-recreate so audit log can diff ---
    prev_rp_state = {field: getattr(real_product, field) for field in _REAL_PRODUCT_FIELDS}
    prev_attrs: dict[str, list[str]] = {}
    for pa in ProductAttribute.objects.filter(product=product).select_related("feature"):
        prev_attrs.setdefault(pa.feature.idx, []).append(str(pa))
    prev_categories = sorted(
        ProductInCategory.objects.filter(product=product)
        .select_related("category")
        .values_list("category__idx", flat=True)
    )
    prev_picture_count = ProductPicture.objects.filter(product=product, is_inherited=False).count()

    # Physical overwrite shared across all channels.
    apply_real_product_mappings(real_product, sp, profile, force_overwrite=True)
    real_product.refresh_from_db()

    # Replace ProductAttribute for mapped features.
    mapped_feature_idxs = [
        m.target_identifier
        for m in profile.attribute_mappings.all()
        if m.target_type == AttributeMappingTargetType.FEATURE.value
    ]
    if mapped_feature_idxs:
        ProductAttribute.objects.filter(product=product, feature__idx__in=mapped_feature_idxs).delete()
    apply_attribute_mappings(product, sp, profile, language)

    # Full replace ProductInCategory.
    ProductInCategory.objects.filter(product=product).delete()
    apply_category_mappings(product, sp, profile, channel_idx)

    # Reset images per channel.
    ProductPicture.objects.filter(product=product, is_inherited=False).delete()
    images_complete = list(sp.images_complete_channel_idxs or [])
    if channel_idx in images_complete:
        images_complete.remove(channel_idx)
        sp.images_complete_channel_idxs = images_complete

    sp.pushed_by = user
    sp.pushed_at = timezone.now()
    sp.save(update_fields=["images_complete_channel_idxs", "pushed_by", "pushed_at", "modified_at"])

    _write_qms_cost_link(sp, source, [channel_idx], event_sink=event_sink)
    _emit_pushed_signal(sp, real_product.sku, channel_idx)

    # --- Audit: emit diff entries for everything overwritten on this channel ---
    drafts: list[dict[str, Any]] = []
    common = {
        "source_product": sp,
        "real_product_sku": real_product.sku,
        "source": ChangeLogSource.FORCE_REPUSH.value,
        "triggered_by": user,
        "applied_to_pim": True,
    }
    for field in _REAL_PRODUCT_FIELDS:
        prev_val = prev_rp_state[field]
        new_val = getattr(real_product, field)
        if prev_val != new_val:
            drafts.append(
                {
                    **common,
                    "field_path": f"physical.{field}",
                    "before": _json_safe(prev_val),
                    "after": _json_safe(new_val),
                }
            )
    new_attrs: dict[str, list[str]] = {}
    for pa in ProductAttribute.objects.filter(product=product).select_related("feature"):
        new_attrs.setdefault(pa.feature.idx, []).append(str(pa))
    for feature_idx in sorted(set(prev_attrs) | set(new_attrs)):
        prev_vals = prev_attrs.get(feature_idx)
        new_vals = new_attrs.get(feature_idx)
        if prev_vals != new_vals:
            drafts.append({**common, "field_path": f"attribute.{feature_idx}", "before": prev_vals, "after": new_vals})
    new_categories = sorted(
        ProductInCategory.objects.filter(product=product)
        .select_related("category")
        .values_list("category__idx", flat=True)
    )
    if list(prev_categories) != list(new_categories):
        drafts.append(
            {
                **common,
                "field_path": f"categories.{channel_idx}",
                "before": list(prev_categories),
                "after": list(new_categories),
            }
        )
    if prev_picture_count > 0:
        # Pictures are repopulated async via image_download_task — record the reset only.
        drafts.append(
            {**common, "field_path": f"pictures.{channel_idx}", "before": {"count": prev_picture_count}, "after": None}
        )
    _flush_audit_drafts(drafts, context="force_repush")
    if resolution.used_fallback:
        _emit_language_fallback(
            sp,
            source,
            profile,
            channel,
            resolution,
            user,
            context="force_repush",
            event_sink=event_sink,
            real_product_sku=real_product.sku,
        )
    return product


__all__ = [
    "LANG_SOURCE_CHANNEL_DEFAULT_FALLBACK",
    "LANG_SOURCE_PROFILE_OVERRIDE",
    "LANG_SOURCE_SOURCE_DEFAULT",
    "LanguageResolution",
    "apply_attribute_mappings",
    "apply_category_mappings",
    "apply_real_product_mappings",
    "force_repush_to_channel",
    "generate_sku",
    "init_push_to_channel",
    "resolve_feature_set",
    "resolve_language",
    "resolve_language_for_channel",
]
