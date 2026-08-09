# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Mapping configuration service for django_atlas.

Owns the SourceMappingProfile + SourceAttributeMapping +
SourceCategoryMapping CRUD plus fail-fast validation against PIM.

Frozen decisions in scope:
  active profiles per source MUST have disjoint target_channel_idxs.
  channel removal emits 'channel_removed_from_profile' IntegrationEvent
        with affected_skus_count (manual cleanup in PIM required).
"""

from typing import Any

from django.db.models import Q, QuerySet
from django_regional.models.language import Language

from django_atlas.enums import MAPPING_VALUE_MODIFIERS, EventSeverity, EventType, MappingValueModifier
from django_atlas.models import (
    AttributeMappingTargetType,
    Source,
    SourceAttributeMapping,
    SourceCategoryMapping,
    SourceMappingProfile,
    SourceProduct,
)
from django_atlas.services import event_service, mapping_validator_service


def _resolve_language(language_id: int | None) -> Language | None:
    if language_id is None:
        return None
    try:
        return Language.objects.get(pk=language_id)
    except Language.DoesNotExist as exc:
        raise ValueError(f"Language with id={language_id} not found") from exc


_REAL_PRODUCT_FIELDS = frozenset({"weight", "ean", "width", "height", "deep", "kind_of_product"})

_PROFILE_EDITABLE_FIELDS = frozenset({"name", "target_channel_idxs", "import_language", "feature_set_idx", "is_active"})
_ATTRIBUTE_MAPPING_EDITABLE_FIELDS = frozenset(
    {"source_field", "target_type", "target_identifier", "is_required", "modifier"}
)
_CATEGORY_MAPPING_EDITABLE_FIELDS = frozenset({"source_field", "source_value", "target_category_idx"})


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


def list_profiles(source_idx: str) -> QuerySet[SourceMappingProfile]:
    return SourceMappingProfile.objects.filter(source__idx=source_idx).order_by("idx")


def get_profile(source_idx: str, profile_idx: str) -> SourceMappingProfile:
    try:
        return SourceMappingProfile.objects.get(source__idx=source_idx, idx=profile_idx)
    except SourceMappingProfile.DoesNotExist as exc:
        raise ValueError(f"Profile '{profile_idx}' not found for source '{source_idx}'") from exc


def get_profile_by_pk(pk: int) -> SourceMappingProfile:
    try:
        return SourceMappingProfile.objects.get(pk=pk)
    except SourceMappingProfile.DoesNotExist as exc:
        raise ValueError(f"Profile with pk={pk} not found") from exc


def list_attribute_mappings(profile_pk: int) -> QuerySet[SourceAttributeMapping]:
    return SourceAttributeMapping.objects.filter(profile_id=profile_pk)


def get_attribute_mapping(profile_pk: int, pk: int) -> SourceAttributeMapping:
    try:
        return SourceAttributeMapping.objects.get(profile_id=profile_pk, pk=pk)
    except SourceAttributeMapping.DoesNotExist as exc:
        raise ValueError(f"AttributeMapping pk={pk} not found in profile {profile_pk}") from exc


def list_category_mappings(profile_pk: int) -> QuerySet[SourceCategoryMapping]:
    return SourceCategoryMapping.objects.filter(profile_id=profile_pk)


def get_category_mapping(profile_pk: int, pk: int) -> SourceCategoryMapping:
    try:
        return SourceCategoryMapping.objects.get(profile_id=profile_pk, pk=pk)
    except SourceCategoryMapping.DoesNotExist as exc:
        raise ValueError(f"CategoryMapping pk={pk} not found in profile {profile_pk}") from exc


def create_profile(
    source_idx: str,
    idx: str,
    name: str,
    target_channel_idxs: list[str],
    *,
    import_language: Language | None = None,
    import_language_id: int | None = None,
    feature_set_idx: str | None = None,
    is_active: bool = True,
) -> SourceMappingProfile:
    """C4: accept either resolved Language or `_id` int. View layer keeps ORM out."""
    if import_language is None and import_language_id is not None:
        import_language = _resolve_language(import_language_id)
    try:
        source = Source.objects.get(idx=source_idx)
    except Source.DoesNotExist as exc:
        raise ValueError(f"Source '{source_idx}' not found") from exc
    _validate_target_channels(target_channel_idxs)
    _validate_channels_disjoint(source_idx, idx, target_channel_idxs, is_active)
    return SourceMappingProfile.objects.create(
        source=source,
        idx=idx,
        name=name,
        target_channel_idxs=list(target_channel_idxs),
        import_language=import_language,
        feature_set_idx=feature_set_idx,
        is_active=is_active,
    )


def update_profile(source_idx: str, profile_idx: str, **fields: Any) -> SourceMappingProfile:
    if "import_language_id" in fields:
        fields["import_language"] = _resolve_language(fields.pop("import_language_id"))
    invalid = set(fields) - _PROFILE_EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_profile: {sorted(invalid)}")
    profile = get_profile(source_idx, profile_idx)
    pre_channels = set(profile.target_channel_idxs or [])

    new_channels = (
        list(fields["target_channel_idxs"]) if "target_channel_idxs" in fields else profile.target_channel_idxs
    )
    new_active = bool(fields["is_active"]) if "is_active" in fields else profile.is_active

    if "target_channel_idxs" in fields or "is_active" in fields:
        _validate_target_channels(new_channels)
        _validate_channels_disjoint(source_idx, profile_idx, new_channels, new_active)

    for key, value in fields.items():
        setattr(profile, key, value)
    profile.save()

    if "target_channel_idxs" in fields:
        removed = pre_channels - set(profile.target_channel_idxs or [])
        if removed:
            _emit_channel_removal_event(profile.source, profile_idx, sorted(removed))
    return profile


def delete_profile(source_idx: str, profile_idx: str) -> None:
    profile = get_profile(source_idx, profile_idx)
    if profile.target_channel_idxs:
        _emit_channel_removal_event(profile.source, profile_idx, sorted(profile.target_channel_idxs))
    profile.delete()


# ---------------------------------------------------------------------------
# Attribute mapping CRUD
# ---------------------------------------------------------------------------


def add_attribute_mapping(
    profile: SourceMappingProfile,
    source_field: str,
    target_type: str,
    target_identifier: str = "",
    is_required: bool = False,
    modifier: str = MappingValueModifier.NONE.value,
) -> SourceAttributeMapping:
    _validate_attribute_mapping_target(target_type, target_identifier)
    _validate_modifier(modifier)
    return SourceAttributeMapping.objects.create(
        profile=profile,
        source_field=source_field,
        target_type=target_type,
        target_identifier=target_identifier,
        is_required=is_required,
        modifier=modifier,
    )


def update_attribute_mapping(pk: int, **fields: Any) -> SourceAttributeMapping:
    invalid = set(fields) - _ATTRIBUTE_MAPPING_EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_attribute_mapping: {sorted(invalid)}")
    try:
        mapping = SourceAttributeMapping.objects.get(pk=pk)
    except SourceAttributeMapping.DoesNotExist as exc:
        raise ValueError(f"AttributeMapping pk={pk} not found") from exc
    new_target_type = fields.get("target_type", mapping.target_type)
    new_target_identifier = fields.get("target_identifier", mapping.target_identifier)
    if "target_type" in fields or "target_identifier" in fields:
        _validate_attribute_mapping_target(new_target_type, new_target_identifier)
    if "modifier" in fields:
        _validate_modifier(fields["modifier"])
    for key, value in fields.items():
        setattr(mapping, key, value)
    mapping.save()
    return mapping


def remove_attribute_mapping(pk: int) -> None:
    SourceAttributeMapping.objects.filter(pk=pk).delete()


# ---------------------------------------------------------------------------
# Category mapping CRUD
# ---------------------------------------------------------------------------


def add_category_mapping(
    profile: SourceMappingProfile, source_field: str, source_value: str, target_category_idx: str
) -> SourceCategoryMapping:
    _validate_category_mapping_target(target_category_idx, profile.target_channel_idxs or [])
    return SourceCategoryMapping.objects.create(
        profile=profile, source_field=source_field, source_value=source_value, target_category_idx=target_category_idx
    )


def update_category_mapping(pk: int, **fields: Any) -> SourceCategoryMapping:
    invalid = set(fields) - _CATEGORY_MAPPING_EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_category_mapping: {sorted(invalid)}")
    try:
        mapping = SourceCategoryMapping.objects.get(pk=pk)
    except SourceCategoryMapping.DoesNotExist as exc:
        raise ValueError(f"CategoryMapping pk={pk} not found") from exc
    new_target = fields.get("target_category_idx", mapping.target_category_idx)
    if "target_category_idx" in fields:
        _validate_category_mapping_target(new_target, mapping.profile.target_channel_idxs or [])
    for key, value in fields.items():
        setattr(mapping, key, value)
    mapping.save()
    return mapping


def remove_category_mapping(pk: int) -> None:
    SourceCategoryMapping.objects.filter(pk=pk).delete()


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------


def validate_profile(source_idx: str, profile_idx: str) -> dict[str, Any]:
    """Profile-wide validation: errors block push, warnings are advisory.

    warnings are now `list[MappingWarning]` (dict shape) — see
    `schemas/responses/mapping.MappingWarning`. Existing string warnings are wrapped
    into the new shape with a stable code so the CMS can render per-row badges.
    """
    profile = get_profile(source_idx, profile_idx)
    errors: list[str] = []
    warnings: list[dict[str, Any]] = []

    try:
        _validate_target_channels(profile.target_channel_idxs or [])
    except ValueError as exc:
        errors.append(str(exc))

    if profile.feature_set_idx:
        try:
            from django_pim.models import FeatureSet

            if not FeatureSet.objects.filter(idx=profile.feature_set_idx).exists():
                errors.append(f"PIM feature_set '{profile.feature_set_idx}' not found")
        except ImportError:
            warnings.append(
                _profile_warning(
                    code="feature_set_check_skipped",
                    message="django_pim FeatureSet not importable — feature_set check skipped",
                )
            )

    attr_mappings = list(profile.attribute_mappings.all())
    cat_mappings = list(profile.category_mappings.all())

    for mapping in attr_mappings:
        try:
            _validate_attribute_mapping_target(mapping.target_type, mapping.target_identifier)
        except ValueError as exc:
            errors.append(f"attribute_mapping[{mapping.pk}] {mapping.source_field}: {exc}")

    target_channels = profile.target_channel_idxs or []
    for mapping in cat_mappings:
        try:
            _validate_category_mapping_target(mapping.target_category_idx, target_channels)
        except ValueError as exc:
            errors.append(f"category_mapping[{mapping.pk}] {mapping.source_field}={mapping.source_value}: {exc}")

    if not attr_mappings and not cat_mappings:
        warnings.append(_profile_warning(code="no_mappings_configured", message="no mappings configured"))

    # language mismatch is a hard error when no import_language override is set
    # and the source's default_language is not declared by the target channel.
    if profile.import_language is None and target_channels:
        from django_pim.models import Channel

        source_lang = profile.source.default_language.iso2
        for ch_idx in target_channels:
            try:
                channel = Channel.objects.prefetch_related("languages").get(idx=ch_idx)
            except Channel.DoesNotExist:
                # Missing channel already flagged by _validate_target_channels above.
                continue
            ch_langs = sorted(channel.languages.values_list("iso2", flat=True))
            if source_lang not in ch_langs:
                errors.append(
                    f"[profile_language_mismatch] channel '{ch_idx}' languages={ch_langs} "
                    f"do not include source '{profile.source.idx}' default_language='{source_lang}'. "
                    f"Set Profile.import_language to a channel-supported language "
                    f"(or push will silently fall back to channel.default_language='{channel.default_language.iso2}')."
                )

    # advisory warnings (typo + type compat)
    warnings.extend(mapping_validator_service.collect_warnings(profile))

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _profile_warning(*, code: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "mapping_kind": "profile",
        "mapping_id": None,
        "source_field": None,
        "source_value": None,
        "target_identifier": None,
        "details": {},
    }


# ---------------------------------------------------------------------------
# Validators (private)
# ---------------------------------------------------------------------------


def _validate_target_channels(target_channel_idxs: list[str]) -> None:
    if not target_channel_idxs:
        return
    from django_pim.models import Channel

    for idx in target_channel_idxs:
        if not Channel.objects.filter(idx=idx).exists():
            raise ValueError(f"PIM channel '{idx}' not found")


def _validate_channels_disjoint(
    source_idx: str, profile_idx: str, target_channel_idxs: list[str], is_active: bool
) -> None:
    if not is_active or not target_channel_idxs:
        return
    new_set = set(target_channel_idxs)
    others = SourceMappingProfile.objects.filter(source__idx=source_idx, is_active=True).exclude(idx=profile_idx)
    for other in others:
        overlap = set(other.target_channel_idxs or []) & new_set
        if overlap:
            raise ValueError(
                f"Channel(s) {sorted(overlap)} already covered by active profile '{other.idx}'. "
                f"Active profiles must have disjoint target_channel_idxs."
            )


def _validate_modifier(modifier: str | None) -> None:
    if modifier is None or modifier == "":
        return
    if modifier not in MAPPING_VALUE_MODIFIERS:
        raise ValueError(
            f"modifier '{modifier}' is not in whitelist (enums.MappingValueModifier). "
            f"Allowed: {sorted(MAPPING_VALUE_MODIFIERS)}"
        )


def _validate_attribute_mapping_target(target_type: str, target_identifier: str) -> None:
    if target_type == AttributeMappingTargetType.SKIP.value:
        return
    if target_type == AttributeMappingTargetType.REAL_PRODUCT.value:
        if target_identifier not in _REAL_PRODUCT_FIELDS:
            raise ValueError(
                f"target_identifier '{target_identifier}' is not a valid RealProduct field. "
                f"Allowed: {sorted(_REAL_PRODUCT_FIELDS)}"
            )
        return
    if target_type == AttributeMappingTargetType.FEATURE.value:
        from django_pim.models.feature import Feature
        from django_pim.services import feature_service

        try:
            feature_service.get_feature_by_idx(target_identifier)
        except Feature.DoesNotExist as exc:
            raise ValueError(f"PIM feature '{target_identifier}' not found") from exc
        return
    raise ValueError(f"target_type '{target_type}' is not valid")


def _validate_category_mapping_target(target_category_idx: str, target_channel_idxs: list[str]) -> None:
    if not target_channel_idxs:
        raise ValueError("MappingProfile must have at least one target channel before adding category mappings")

    from django_pim.models.channel import Channel
    from django_pim.models.product_category import ProductCategory
    from django_pim.services import category_service

    matched = 0
    for channel_idx in target_channel_idxs:
        try:
            category_service.get_category_by_idx(channel_idx, target_category_idx)
            matched += 1
        except (ProductCategory.DoesNotExist, Channel.DoesNotExist):
            # hardening: if the target channel was removed from PIM the lookup
            # raises Channel.DoesNotExist. Treat it like a missing category — validate
            # already collects errors for the missing PIM channel at the profile level.
            continue

    if matched == 0:
        raise ValueError(
            f"PIM category '{target_category_idx}' not found in any target channel: {sorted(target_channel_idxs)}"
        )


def _emit_channel_removal_event(source: Source, profile_idx: str, removed_channel_idxs: list[str]) -> None:
    overlap_q = Q()
    for channel_idx in removed_channel_idxs:
        overlap_q |= Q(pushed_to_channel_idxs__contains=[channel_idx])
    affected_skus_count = SourceProduct.objects.filter(source=source).filter(overlap_q).count()
    event_service.record(
        event_type=EventType.CHANNEL_REMOVED_FROM_PROFILE.value,
        severity=EventSeverity.WARNING.value,
        source=source,
        message=(
            f"Profile '{profile_idx}' lost channels: {removed_channel_idxs}. "
            f"{affected_skus_count} SP affected — manual cleanup in PIM required."
        ),
        details={
            "profile_idx": profile_idx,
            "removed_channel_idxs": removed_channel_idxs,
            "affected_skus_count": affected_skus_count,
        },
    )
