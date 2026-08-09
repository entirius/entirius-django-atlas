# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for audit_log emission from pim_writer init_push + force_repush."""

from decimal import Decimal

import pytest

from django_atlas.enums import ChangeLogSource
from django_atlas.models import AttributeMappingTargetType, SourceProductChangeLog
from django_atlas.services import pim_writer
from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _register(feature, feature_set):
    from django_pim.models.feature_set import FeatureInFeatureSet

    FeatureInFeatureSet.objects.get_or_create(feature=feature, feature_set=feature_set)


def test_init_push_emits_baseline_snapshot(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-init-audit")
    fs = pim_feature_set_factory("fs-init-audit")
    feat = pim_typed_feature_factory("nm", int(FeatureTypeEnum.TEXT))
    _register(feat, fs)
    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, name="Widget", external_id="ext-baseline", cost=Decimal("42.00"), stock=7)
    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    baseline = SourceProductChangeLog.objects.get(source_product=sp, source=ChangeLogSource.INIT_PUSH.value)
    assert baseline.field_path == f"snapshot.{channel.idx}"
    assert baseline.applied_to_pim is True
    assert baseline.triggered_by == admin_user
    assert baseline.real_product_sku == product.real_product.sku
    assert baseline.after["name"] == "Widget"
    assert Decimal(baseline.after["cost"]) == Decimal("42.00")
    assert baseline.after["stock"] == 7


def test_force_repush_emits_attribute_diff(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-frepush-audit")
    fs = pim_feature_set_factory("fs-frepush-audit")
    feat = pim_typed_feature_factory("title", int(FeatureTypeEnum.TEXT))
    _register(feat, fs)
    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, name="Original", external_id="ext-frepush")
    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    # Mutate SP and force-repush
    sp.name = "Renamed"
    sp.save(update_fields=["name", "modified_at"])
    pim_writer.force_repush_to_channel(sp, profile, channel.idx, admin_user)

    repush_entries = SourceProductChangeLog.objects.filter(source_product=sp, source=ChangeLogSource.FORCE_REPUSH.value)
    attr_entries = repush_entries.filter(field_path=f"attribute.{feat.idx}")
    assert attr_entries.exists()
    entry = attr_entries.first()
    assert entry.applied_to_pim is True
    assert entry.triggered_by == admin_user


def test_force_repush_emits_physical_diff_when_real_product_field_overwritten(
    admin_user, language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-frepush-phys")
    fs = pim_feature_set_factory("fs-frepush-phys")
    feat = pim_typed_feature_factory("anchor", int(FeatureTypeEnum.TEXT))
    _register(feat, fs)
    sup = SourceFactory(default_language=language, default_currency=currency, default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[channel.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    AttributeMappingFactory(
        profile=profile,
        source_field="weight",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
    )
    sp = SourceProductFactory(source=sup, external_id="ext-phys-rp", data={"weight": "1.250"})
    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    # Mutate physical attribute and force-repush
    sp.data = {"weight": "2.750"}
    sp.save(update_fields=["data", "modified_at"])
    pim_writer.force_repush_to_channel(sp, profile, channel.idx, admin_user)

    phys = SourceProductChangeLog.objects.filter(
        source_product=sp, source=ChangeLogSource.FORCE_REPUSH.value, field_path="physical.weight"
    )
    assert phys.exists()
    assert phys.first().applied_to_pim is True
