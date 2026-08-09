# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""unit tests for mapping_validator_service.collect_warnings()."""

import pytest
from django.core.cache import cache

from django_atlas.models import AttributeMappingTargetType
from django_atlas.services import mapping_validator_service

from .factories import (
    AttributeMappingFactory,
    CategoryMappingFactory,
    MappingProfileFactory,
    SourceFactory,
    SourceProductFactory,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Category mapping — source_value presence
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_source_value_present_in_source_data_no_warning(language, currency):
    source = SourceFactory(idx="catok", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"category_path": "Pozostałe"})
    profile = MappingProfileFactory(source=source)
    CategoryMappingFactory(
        profile=profile, source_field="category_path", source_value="Pozostałe", target_category_idx="sale"
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    assert warnings == []


@pytest.mark.django_db
def test_source_value_typo_emits_warning_with_suggestion(language, currency):
    source = SourceFactory(idx="cattypo", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"category_path": "Pozostałe"})
    SourceProductFactory(source=source, external_id="b", data={"category_path": "Pozostałe"})
    profile = MappingProfileFactory(source=source)
    cm = CategoryMappingFactory(
        profile=profile,
        source_field="category_path",
        source_value="Pozostalee",  # typo
        target_category_idx="sale",
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    assert len(warnings) == 1
    w = warnings[0]
    assert w["code"] == "source_value_not_found_in_source_data"
    assert w["mapping_kind"] == "category"
    assert w["mapping_id"] == cm.pk
    assert w["source_field"] == "category_path"
    assert w["source_value"] == "Pozostalee"
    assert w["details"]["suggestion"] == "Pozostałe"


# ---------------------------------------------------------------------------
# Attribute mapping — type compatibility
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_decimal_feature_with_numeric_samples_no_warning(language, currency, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum

    source = SourceFactory(idx="dec-ok", default_language=language, default_currency=currency)
    for i in range(5):
        SourceProductFactory(source=source, external_id=f"sku-{i}", data={"weight_g": f"{100 + i}"})
    profile = MappingProfileFactory(source=source)
    pim_typed_feature_factory("weight", feature_type=FeatureTypeEnum.DECIMAL)
    AttributeMappingFactory(
        profile=profile,
        source_field="weight_g",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier="weight",
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    assert warnings == []


@pytest.mark.django_db
def test_bool_feature_with_numeric_samples_emits_type_warning(language, currency, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum

    source = SourceFactory(idx="bool-mismatch", default_language=language, default_currency=currency)
    for i in range(5):
        SourceProductFactory(source=source, external_id=f"sku-{i}", data={"weight_g": f"{350 + i}"})
    profile = MappingProfileFactory(source=source)
    pim_typed_feature_factory("is_fragile", feature_type=FeatureTypeEnum.BOOL)
    am = AttributeMappingFactory(
        profile=profile,
        source_field="weight_g",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier="is_fragile",
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    type_warnings = [w for w in warnings if w["code"] == "type_incompatibility"]
    assert len(type_warnings) == 1
    w = type_warnings[0]
    assert w["mapping_kind"] == "attribute"
    assert w["mapping_id"] == am.pk
    assert w["source_field"] == "weight_g"
    assert w["target_identifier"] == "is_fragile"
    assert w["details"]["failed_pct"] > 50
    assert w["details"]["expected_type"] == "bool"


@pytest.mark.django_db
def test_text_t9n_feature_always_compatible(language, currency, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum

    source = SourceFactory(idx="text-ok", default_language=language, default_currency=currency)
    for i in range(5):
        SourceProductFactory(source=source, external_id=f"sku-{i}", data={"name_src": f"Anything {i}"})
    profile = MappingProfileFactory(source=source)
    pim_typed_feature_factory("loose_text", feature_type=FeatureTypeEnum.TEXT_T9N)
    AttributeMappingFactory(
        profile=profile,
        source_field="name_src",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier="loose_text",
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    assert warnings == []


@pytest.mark.django_db
def test_skip_target_type_never_emits_type_warning(language, currency):
    source = SourceFactory(idx="skip-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="sku-0", data={"weight_g": "350"})
    profile = MappingProfileFactory(source=source)
    AttributeMappingFactory(
        profile=profile,
        source_field="weight_g",
        target_type=AttributeMappingTargetType.SKIP.value,
        target_identifier="",
    )

    warnings = mapping_validator_service.collect_warnings(profile)

    assert warnings == []


@pytest.mark.django_db
def test_empty_profile_no_warnings(language, currency):
    source = SourceFactory(idx="empty-profile-sup", default_language=language, default_currency=currency)
    profile = MappingProfileFactory(source=source)

    warnings = mapping_validator_service.collect_warnings(profile)

    assert warnings == []
