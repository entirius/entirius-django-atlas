# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from decimal import Decimal

import pytest

from django_atlas.enums import EventType, ProductStatus
from django_atlas.models import AttributeMappingTargetType, IntegrationEvent
from django_atlas.services import pim_writer
from tests.factories import (
    AttributeMappingFactory,
    CategoryMappingFactory,
    FeedFactory,
    MappingProfileFactory,
    SourceFactory,
    SourceProductFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers (1-8)
# ---------------------------------------------------------------------------


def test_generate_sku_deterministic():
    sup = SourceFactory(sku_prefix="ACME")
    a = pim_writer.generate_sku(sup, "EXT-1")
    b = pim_writer.generate_sku(sup, "EXT-1")
    assert a == b
    assert a.startswith("ACME-")


def test_resolve_feature_set_sp_override_wins():
    sup = SourceFactory(default_feature_set_idx="fs-source")
    feed = FeedFactory(source=sup, feature_set_idx="fs-feed")
    profile = MappingProfileFactory(source=sup, feature_set_idx="fs-profile")
    sp = SourceProductFactory(source=sup, feed=feed, feature_set_idx_override="fs-sp")
    assert pim_writer.resolve_feature_set(sp, feed, profile, sup) == "fs-sp"


def test_resolve_feature_set_feed_wins_when_no_sp_override():
    sup = SourceFactory(default_feature_set_idx="fs-source")
    feed = FeedFactory(source=sup, feature_set_idx="fs-feed")
    profile = MappingProfileFactory(source=sup, feature_set_idx="fs-profile")
    sp = SourceProductFactory(source=sup, feed=feed, feature_set_idx_override=None)
    assert pim_writer.resolve_feature_set(sp, feed, profile, sup) == "fs-feed"


def test_resolve_feature_set_profile_wins_when_no_sp_no_feed():
    sup = SourceFactory(default_feature_set_idx="fs-source")
    profile = MappingProfileFactory(source=sup, feature_set_idx="fs-profile")
    sp = SourceProductFactory(source=sup, feature_set_idx_override=None)
    assert pim_writer.resolve_feature_set(sp, None, profile, sup) == "fs-profile"


def test_resolve_feature_set_falls_back_to_source_default():
    sup = SourceFactory(default_feature_set_idx="fs-source")
    profile = MappingProfileFactory(source=sup, feature_set_idx=None)
    sp = SourceProductFactory(source=sup, feature_set_idx_override=None)
    assert pim_writer.resolve_feature_set(sp, None, profile, sup) == "fs-source"


def test_resolve_feature_set_raises_when_no_candidate():
    sup = SourceFactory(default_feature_set_idx=None)
    profile = MappingProfileFactory(source=sup, feature_set_idx=None)
    sp = SourceProductFactory(source=sup, feature_set_idx_override=None)
    with pytest.raises(ValueError, match="no feature_set"):
        pim_writer.resolve_feature_set(sp, None, profile, sup)


def test_resolve_language_uses_profile_import_language(language):
    sup = SourceFactory(default_language=language)
    profile = MappingProfileFactory(source=sup, import_language=language)
    assert pim_writer.resolve_language(profile, sup) == language.iso2


def test_resolve_language_falls_back_to_source_default(language):
    sup = SourceFactory(default_language=language)
    profile = MappingProfileFactory(source=sup, import_language=None)
    assert pim_writer.resolve_language(profile, sup) == language.iso2


# ---------------------------------------------------------------------------
# apply_attribute_mappings (9-14, 19-20, 22)
# ---------------------------------------------------------------------------


def _register_feature_in_feature_set(feature, feature_set):
    """Wire feature into feature_set so PIM ProductAttribute.validate_feature() passes."""
    from django_pim.models.feature_set import FeatureInFeatureSet

    FeatureInFeatureSet.objects.get_or_create(feature=feature, feature_set=feature_set)


def _make_product_for_attributes(
    pim_channel_factory, pim_feature_set_factory, sku="sku-1", features=None, channel_idx="ch-x"
):
    """Create a minimal Product+Channel+FeatureSet+RealProduct so we can attach ProductAttribute rows.

    features: optional iterable of Feature instances to register in the feature_set.
    """
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.real_product import RealProduct

    channel = pim_channel_factory(channel_idx)
    fs = pim_feature_set_factory()
    if features:
        for feat in features:
            _register_feature_in_feature_set(feat, fs)
    rp = RealProduct.objects.create(sku=sku)
    product = Product.objects.create(
        real_product=rp,
        shop=channel,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    return product


def test_apply_attribute_mappings_text(pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    feature = pim_typed_feature_factory("text-feat", int(FeatureTypeEnum.TEXT))
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-text", features=[feature], channel_idx="ch-text"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="description",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
    )
    sp = SourceProductFactory(data={"description": "Hello"}, status=ProductStatus.APPROVED.value)

    pim_writer.apply_attribute_mappings(product, sp, profile, language="en")

    pa = ProductAttribute.objects.get(product=product, feature=feature)
    assert pa.value_txt == "Hello"


def test_apply_attribute_mappings_text_t9n(pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    feature = pim_typed_feature_factory("desc-t9n", int(FeatureTypeEnum.TEXT_T9N))
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-t9n", features=[feature], channel_idx="ch-t9n"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="description",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
    )
    sp = SourceProductFactory(data={"description": "Witaj"})

    pim_writer.apply_attribute_mappings(product, sp, profile, language="pl")

    pa = ProductAttribute.objects.get(product=product, feature=feature)
    assert pa.value_txt_t9n == {"pl": "Witaj"}


def test_apply_attribute_mappings_decimal(pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    feature = pim_typed_feature_factory("weight-feat", int(FeatureTypeEnum.DECIMAL))
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-dec", features=[feature], channel_idx="ch-dec"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="weight",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
    )
    sp = SourceProductFactory(data={"weight": "1.50"})

    pim_writer.apply_attribute_mappings(product, sp, profile, language="en")

    pa = ProductAttribute.objects.get(product=product, feature=feature)
    assert pa.value_decimal == Decimal("1.50")


def test_apply_attribute_mappings_select_lookup(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, pim_attribute_factory
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    feature = pim_typed_feature_factory("color", int(FeatureTypeEnum.SELECT))
    red = pim_attribute_factory(feature, "red")
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-sel", features=[feature], channel_idx="ch-sel"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="color",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
    )
    sp = SourceProductFactory(data={"color": "red"})

    pim_writer.apply_attribute_mappings(product, sp, profile, language="en")

    pa = ProductAttribute.objects.get(product=product, feature=feature)
    assert pa.attribute_id == red.id


def test_apply_attribute_mappings_required_missing_raises(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum

    feature = pim_typed_feature_factory("must-have", int(FeatureTypeEnum.TEXT))
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-req", features=[feature], channel_idx="ch-req"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="brand",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
        is_required=True,
    )
    sp = SourceProductFactory(data={})  # no 'brand' key

    with pytest.raises(ValueError, match="required field"):
        pim_writer.apply_attribute_mappings(product, sp, profile, language="en")


def test_apply_attribute_mappings_optional_missing_emits_event(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum

    feature = pim_typed_feature_factory("optional", int(FeatureTypeEnum.TEXT))
    product = _make_product_for_attributes(
        pim_channel_factory, pim_feature_set_factory, sku="sku-opt", features=[feature], channel_idx="ch-opt"
    )
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="manufacturer",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
        is_required=False,
    )
    sp = SourceProductFactory(data={})

    pim_writer.apply_attribute_mappings(product, sp, profile, language="en")

    assert IntegrationEvent.objects.filter(
        event_type=EventType.ATTRIBUTE_VALUE_MISSING.value, source_product=sp
    ).exists()


# ---------------------------------------------------------------------------
# apply_real_product_mappings (15) + apply_category_mappings (16-17)
# ---------------------------------------------------------------------------


def test_apply_real_product_mappings_init_preserves_force_overwrites(pim_channel_factory):
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="sku-rp", weight=Decimal("1.00"))
    profile = MappingProfileFactory()
    AttributeMappingFactory(
        profile=profile,
        source_field="weight",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
    )
    sp = SourceProductFactory(data={"weight": "2.50"})

    # INIT: must NOT overwrite existing 1.00
    changed_init = pim_writer.apply_real_product_mappings(rp, sp, profile, force_overwrite=False)
    rp.refresh_from_db()
    assert changed_init is False
    assert rp.weight == Decimal("1.00")

    # Force overwrite: replace
    changed_force = pim_writer.apply_real_product_mappings(rp, sp, profile, force_overwrite=True)
    rp.refresh_from_db()
    assert changed_force is True
    assert rp.weight == Decimal("2.50")


def test_apply_category_mappings_happy_path(pim_channel_factory, pim_category_factory):
    from django_pim.models.product_in_category import ProductInCategory

    channel = pim_channel_factory("ch-cat")
    cat = pim_category_factory(channel, "cat-a")
    profile = MappingProfileFactory(target_channel_idxs=[channel.idx])
    CategoryMappingFactory(profile=profile, source_field="category", source_value="A", target_category_idx=cat.idx)
    sp = SourceProductFactory(data={"category": "A"})

    from django_pim.models.feature_set import FeatureSet
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="sku-cat")
    fs, _ = FeatureSet.objects.get_or_create(idx="fs-cat", defaults={"name": "fs"})
    product = Product.objects.create(
        real_product=rp,
        shop=channel,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    pim_writer.apply_category_mappings(product, sp, profile, channel.idx)

    assert ProductInCategory.objects.filter(product=product, category=cat).exists()


def test_apply_category_mappings_missing_emits_event(pim_channel_factory):
    channel = pim_channel_factory("ch-nocat")
    profile = MappingProfileFactory(target_channel_idxs=[channel.idx])
    CategoryMappingFactory(profile=profile, source_field="category", source_value="X", target_category_idx="ghost")
    sp = SourceProductFactory(data={"category": "X"})

    from django_pim.models.feature_set import FeatureSet
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="sku-noc")
    fs, _ = FeatureSet.objects.get_or_create(idx="fs-noc", defaults={"name": "fs"})
    product = Product.objects.create(
        real_product=rp,
        shop=channel,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    pim_writer.apply_category_mappings(product, sp, profile, channel.idx)

    assert IntegrationEvent.objects.filter(event_type=EventType.PIM_CATEGORY_MISSING.value, source_product=sp).exists()


# ---------------------------------------------------------------------------
# init_push_to_channel (18) + multi_source_overlap (21) + SELECT defensive (22) + MULTISELECT (19-20)
# ---------------------------------------------------------------------------


def test_init_push_to_channel_creates_real_product_and_product(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product import Product
    from django_pim.models.product_attribute import ProductAttribute
    from django_pim.models.real_product import RealProduct

    channel = pim_channel_factory("ch-init")
    fs = pim_feature_set_factory("fs-init")
    feat = pim_typed_feature_factory("nm", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, name="Widget", external_id="ext-init")

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert RealProduct.objects.filter(sku=product.real_product.sku).exists()
    assert Product.objects.filter(real_product=product.real_product, shop=channel).exists()
    assert ProductAttribute.objects.filter(product=product, feature=feat).exists()
    sp.refresh_from_db()
    assert channel.idx in sp.pushed_to_channel_idxs


@pytest.mark.django_db
@pytest.mark.parametrize("source_default,expected_class", [(1, 1), (2, 2), (0, 0)])
def test_init_push_uses_source_default_product_class(
    admin_user,
    language,
    currency,
    pim_channel_factory,
    pim_feature_set_factory,
    pim_typed_feature_factory,
    source_default,
    expected_class,
):
    # Product.product_class now comes from Source.default_product_class,
    # not the hardcoded ProductBase (0).
    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory(f"ch-pclass-{source_default}")
    fs = pim_feature_set_factory(f"fs-pclass-{source_default}")
    feat = pim_typed_feature_factory(f"nm-{source_default}", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(
        default_language=language,
        default_currency=currency,
        default_feature_set_idx=fs.idx,
        default_product_class=source_default,
    )
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, name="Widget", external_id=f"ext-pc-{source_default}")

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert product.product_class == expected_class


def test_init_multiselect_happy_path(
    admin_user,
    language,
    currency,
    pim_channel_factory,
    pim_feature_set_factory,
    pim_typed_feature_factory,
    pim_attribute_factory,
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    channel = pim_channel_factory("ch-msel")
    fs = pim_feature_set_factory("fs-msel")
    feat = pim_typed_feature_factory("colors", int(FeatureTypeEnum.MULTISELECT))
    _register_feature_in_feature_set(feat, fs)
    pim_attribute_factory(feat, "color-red")
    pim_attribute_factory(feat, "color-blue")
    pim_attribute_factory(feat, "color-green")

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="colors",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(
        source=sup, external_id="ext-msel", data={"colors": ["color-red", "color-blue", "color-green"]}
    )

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert ProductAttribute.objects.filter(product=product, feature=feat).count() == 3


def test_init_multiselect_partial_missing_skips_and_logs(
    admin_user,
    language,
    currency,
    pim_channel_factory,
    pim_feature_set_factory,
    pim_typed_feature_factory,
    pim_attribute_factory,
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    channel = pim_channel_factory("ch-msel-miss")
    fs = pim_feature_set_factory("fs-msel-miss")
    feat = pim_typed_feature_factory("colors2", int(FeatureTypeEnum.MULTISELECT))
    _register_feature_in_feature_set(feat, fs)
    pim_attribute_factory(feat, "color-red")
    pim_attribute_factory(feat, "color-green")
    # 'unknown-color' deliberately NOT created.

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="colors2",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(
        source=sup, external_id="ext-msel-miss", data={"colors2": ["color-red", "unknown-color", "color-green"]}
    )

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert ProductAttribute.objects.filter(product=product, feature=feat).count() == 2
    events = IntegrationEvent.objects.filter(event_type=EventType.PIM_ATTRIBUTE_MISSING.value, source_product=sp)
    assert events.count() == 1
    assert "unknown-color" in events.first().message


def test_multi_source_overlap_event_fires_on_second_push(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """When source B pushes a SKU already linked to source A,
    multi_source_overlap info event fires (rp_created=False + SourceProductLink
    exists for another source).
    """
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.real_product import RealProduct

    channel = pim_channel_factory("ch-overlap")
    fs = pim_feature_set_factory("fs-overlap")
    feat = pim_typed_feature_factory("nm-ov", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup_a = SourceFactory(
        default_language=language, default_currency=currency, default_feature_set_idx=fs.idx, sku_prefix="OV"
    )
    sup_b = SourceFactory(
        default_language=language, default_currency=currency, default_feature_set_idx=fs.idx, sku_prefix="OV"
    )

    profile_a = MappingProfileFactory(source=sup_a, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile_a,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp_a = SourceProductFactory(source=sup_a, external_id="overlap-1")
    pim_writer.init_push_to_channel(sp_a, profile_a, channel.idx, admin_user)

    assert IntegrationEvent.objects.filter(event_type=EventType.MULTI_SOURCE_OVERLAP.value).count() == 0

    pim_channel_factory("ch-overlap-2")
    profile_b = MappingProfileFactory(source=sup_b, target_channel_idxs=["ch-overlap-2"], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile_b,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp_b = SourceProductFactory(source=sup_b, external_id="overlap-1")
    pim_writer.init_push_to_channel(sp_b, profile_b, "ch-overlap-2", admin_user)

    assert RealProduct.objects.filter(sku=sp_a.real_product.sku).count() == 1
    overlap_events = IntegrationEvent.objects.filter(event_type=EventType.MULTI_SOURCE_OVERLAP.value)
    assert overlap_events.count() == 1
    event = overlap_events.first()
    assert event.details["new_source_idx"] == sup_b.idx
    assert sup_a.idx in event.details["existing_source_idxs"]


def test_select_with_list_value_emits_warning(
    admin_user,
    language,
    currency,
    pim_channel_factory,
    pim_feature_set_factory,
    pim_typed_feature_factory,
    pim_attribute_factory,
):
    """Defensive: SELECT feature receiving a list (data error) → warning + skip mapping, no crash."""
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    channel = pim_channel_factory("ch-sel-bad")
    fs = pim_feature_set_factory("fs-sel-bad")
    feat = pim_typed_feature_factory("size", int(FeatureTypeEnum.SELECT))
    _register_feature_in_feature_set(feat, fs)
    pim_attribute_factory(feat, "size-l")

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="size",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, external_id="ext-sel-bad", data={"size": ["size-l", "size-m"]})

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert ProductAttribute.objects.filter(product=product, feature=feat).count() == 0
    assert IntegrationEvent.objects.filter(event_type=EventType.PIM_ATTRIBUTE_MISSING.value, source_product=sp).exists()


# ---------------------------------------------------------------------------
# Integration: pim_writer wires qms_writer + pricemanager_writer + product_link_service
# ---------------------------------------------------------------------------


def test_init_push_invokes_qms_writer(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from unittest.mock import patch

    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-qms-spy")
    fs = pim_feature_set_factory("fs-qms-spy")
    feat = pim_typed_feature_factory("nm-qms", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, external_id="qms-spy-1")

    with (
        patch("django_atlas.services.pim_writer.qms_writer.write_stock")
        if False
        else patch("django_atlas.services.qms_writer.write_stock") as spy
    ):
        pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    spy.assert_called_once()
    args, kwargs = spy.call_args
    assert args[0].id == sp.id
    assert args[2] == [channel.idx]


def test_init_push_invokes_pricemanager_writer(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from unittest.mock import patch

    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-pm-spy")
    fs = pim_feature_set_factory("fs-pm-spy")
    feat = pim_typed_feature_factory("nm-pm", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, external_id="pm-spy-1")

    with patch("django_atlas.services.pricemanager_writer.log_cost") as spy:
        pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    spy.assert_called_once()


def test_init_push_creates_source_product_link_with_first_link_primary(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """1st link for a SKU becomes is_primary=True at creation.

    The operator-flags invariant is preserved on UPDATE (see test_force_repush_does_not_reset_operator_is_primary).
    The default flip on CREATE was required so the cost subscriber stops ignoring
    single-source links.
    """
    from django_pim.models.feature import FeatureTypeEnum

    from django_atlas.models import SourceProductLink

    channel = pim_channel_factory("ch-link-d")
    fs = pim_feature_set_factory("fs-link-d")
    feat = pim_typed_feature_factory("nm-l", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, external_id="link-d-1")
    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    link = SourceProductLink.objects.get(real_product_sku=sp.real_product.sku, source=sup)
    assert link.is_primary is True  # 1st link auto-primary
    assert link.external_id == "link-d-1"


def test_force_repush_does_not_reset_operator_is_primary(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """force_repush_to_channel calls upsert_for_push which MUST NOT touch is_primary."""
    from django.db import transaction
    from django_pim.models.feature import FeatureTypeEnum

    from django_atlas.models import SourceProductLink
    from django_atlas.services import push_service

    channel = pim_channel_factory("ch-frp-link")
    fs = pim_feature_set_factory("fs-frp-link")
    feat = pim_typed_feature_factory("nm-frpl", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="frp-link-1")

    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()

    # Operator marks the link as primary.
    link = SourceProductLink.objects.get(real_product_sku=sp.real_product.sku, source=sup)
    link.is_primary = True
    link.save(update_fields=["is_primary"])

    # Force re-push must not reset the operator's flag.
    push_service.force_repush_source_product(sp.id, admin_user)
    link.refresh_from_db()
    assert link.is_primary is True


def test_init_push_works_when_qms_unavailable(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """Soft dep: missing django_qms → push completes, info event recorded, no crash."""
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.product_attribute import ProductAttribute

    channel = pim_channel_factory("ch-soft-qms")
    fs = pim_feature_set_factory("fs-soft-qms")
    feat = pim_typed_feature_factory("nm-soft", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feat, fs)

    sup = SourceFactory(
        default_language=language,
        default_currency=currency,
        default_feature_set_idx=fs.idx,
        target_warehouse_code="WH1",
    )
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, external_id="soft-qms-1")

    # Default: django_qms NOT installed in test env (see tests/settings.py).
    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    # Push succeeded: ProductAttribute exists.
    assert ProductAttribute.objects.filter(product=product, feature=feat).exists()
    # Perf #9: missing QMS is now a silent skip (logger warning only). No per-SP event.
    assert not IntegrationEvent.objects.filter(
        event_type=EventType.QMS_NOT_INSTALLED_SKIPPED.value, source_product=sp
    ).exists()
