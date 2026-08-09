# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for SourceAttributeMapping.modifier on the push path."""

from decimal import Decimal

import pytest

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType, MappingValueModifier, ProductStatus
from django_atlas.models import AttributeMappingTargetType, IntegrationEvent, SourceProductChangeLog
from django_atlas.services import pim_writer
from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def test_grams_to_kg_writes_transformed_weight_and_audit_row(pim_channel_factory):
    """The motivating scenario — a source ships weight_g=7800, push stores RealProduct.weight=7.8."""
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="sku-rp-modifier")
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="weight_g",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
        modifier=MappingValueModifier.GRAMS_TO_KG.value,
    )
    sp = SourceProductFactory(data={"weight_g": 7800}, status=ProductStatus.APPROVED.value)

    changed = pim_writer.apply_real_product_mappings(rp, sp, profile, force_overwrite=False)
    rp.refresh_from_db()

    assert changed is True
    assert rp.weight == Decimal("7.8")

    audit_rows = SourceProductChangeLog.objects.filter(
        source_product=sp, source=ChangeLogSource.MAPPING_TRANSFORM.value
    )
    assert audit_rows.count() == 1
    row = audit_rows.get()
    assert row.field_path == "real_product.weight"
    assert row.before == 7800
    assert row.after == "7.8"


def test_string_uppercase_modifier_applied_on_feature_attribute(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """STRING_UPPERCASE modifier on a TEXT feature uppercases the source value before write."""
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.product_attribute import ProductAttribute
    from django_pim.models.real_product import RealProduct

    feature = pim_typed_feature_factory("modifier-text", int(FeatureTypeEnum.TEXT))
    channel = pim_channel_factory("ch-modifier")
    fs = pim_feature_set_factory()
    from django_pim.models.feature_set import FeatureInFeatureSet

    FeatureInFeatureSet.objects.get_or_create(feature=feature, feature_set=fs)
    rp = RealProduct.objects.create(sku="sku-string-mod")
    product = Product.objects.create(
        real_product=rp,
        shop=channel,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="color",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
        modifier=MappingValueModifier.STRING_UPPERCASE.value,
    )
    sp = SourceProductFactory(data={"color": "blue"}, status=ProductStatus.APPROVED.value)

    pim_writer.apply_attribute_mappings(product, sp, profile, language="en")

    pa = ProductAttribute.objects.get(product=product, feature=feature)
    assert pa.value_txt == "BLUE"

    audit_rows = SourceProductChangeLog.objects.filter(
        source_product=sp, source=ChangeLogSource.MAPPING_TRANSFORM.value
    )
    assert audit_rows.count() == 1
    row = audit_rows.get()
    assert row.field_path == f"feature.{feature.idx}"
    assert row.before == "blue"
    assert row.after == "BLUE"


def test_numeric_modifier_on_garbage_string_emits_warning_event_and_does_not_crash(pim_channel_factory):
    """Type mismatch path — operator misconfigured grams_to_kg on a string field. Push must not crash;
    a warning IntegrationEvent must surface; the raw value (string) cannot coerce to Decimal so
    the field is skipped (no write, no audit row for transform)."""
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="sku-rp-typemismatch")
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="weight_text",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
        modifier=MappingValueModifier.GRAMS_TO_KG.value,
    )
    sp = SourceProductFactory(data={"weight_text": "heavy"}, status=ProductStatus.APPROVED.value)

    changed = pim_writer.apply_real_product_mappings(rp, sp, profile, force_overwrite=False)
    rp.refresh_from_db()

    assert changed is False
    assert rp.weight is None

    warnings = IntegrationEvent.objects.filter(
        source_product=sp, event_type=EventType.MAPPING_TRANSFORM_FAILED.value, severity=EventSeverity.WARNING.value
    )
    assert warnings.count() == 1
    event = warnings.get()
    assert event.details["modifier"] == "grams_to_kg"
    assert event.details["failure_reason"] == "invalid_decimal"
    assert event.details["raw_value"] == "heavy"

    audit_rows = SourceProductChangeLog.objects.filter(
        source_product=sp, source=ChangeLogSource.MAPPING_TRANSFORM.value
    )
    assert audit_rows.count() == 0
