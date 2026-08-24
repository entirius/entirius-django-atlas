# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Push honours an existing `SourceProduct.real_product` instead of generating a second SKU.

Whatever attached the SP (the lookup UI, an enrichment proposal, an earlier push) picked the target.
Pushing to a freshly generated SKU afterwards would create exactly the duplicate RealProduct the
link exists to prevent — and `_persist_sp_after_push` only fills the FK when it is NULL, so the two
would never converge again.
"""

from decimal import Decimal

import pytest
from django_pim.models.product import Product
from django_pim.models.real_product import RealProduct

from django_atlas.enums import EventType
from django_atlas.models import AttributeMappingTargetType, IntegrationEvent
from django_atlas.services import pim_writer
from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

_EAN = "5906214804074"


def _register_feature_in_feature_set(feature, feature_set):
    from django_pim.models.feature_set import FeatureInFeatureSet

    FeatureInFeatureSet.objects.get_or_create(feature=feature, feature_set=feature_set)


@pytest.fixture
def scaffolding(language, currency, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    """Channel + feature set + a source whose profile maps `__name__` and `weight`."""
    from django_pim.models.feature import FeatureTypeEnum

    channel = pim_channel_factory("ch-linked-push")
    feature_set = pim_feature_set_factory("fs-linked-push")
    feature = pim_typed_feature_factory("nm-lp", int(FeatureTypeEnum.TEXT))
    _register_feature_in_feature_set(feature, feature_set)

    source = SourceFactory(
        idx="acme-lp",
        sku_prefix="AC",
        default_language=language,
        default_currency=currency,
        default_feature_set_idx=feature_set.idx,
    )
    profile = MappingProfileFactory(source=source, target_channel_idxs=[channel.idx], feature_set_idx=feature_set.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feature.idx,
    )
    AttributeMappingFactory(
        profile=profile,
        source_field="weight",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
    )
    return channel, profile, source


@pytest.fixture
def linked_real_product():
    """The RealProduct an operator picked in the lookup UI — a PIM SKU, not an atlas-generated one."""
    return RealProduct.objects.create(sku="PIM-LINKED-01", ean=_EAN, weight=Decimal("0.150"))


def _pre_link_events():
    return IntegrationEvent.objects.filter(
        event_type=EventType.PUSHED_ONTO_LINKED_REALPRODUCT.value, details__matched_via="existing_link"
    )


def test_linked_sp_pushes_onto_its_realproduct(admin_user, scaffolding, linked_real_product):
    channel, profile, source = scaffolding
    sp = SourceProductFactory(
        source=source, external_id="6620", ean=_EAN, data={"weight": "0.150"}, real_product=linked_real_product
    )

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert RealProduct.objects.count() == 1
    assert product.real_product_id == linked_real_product.id
    sp.refresh_from_db()
    assert sp.real_product_id == linked_real_product.id
    assert sp.pushed_to_channel_idxs == [channel.idx]


def test_linked_sp_push_does_not_inflate_the_auto_ean_link_event(admin_user, scaffolding, linked_real_product):
    """`auto_linked_to_existing_realproduct` is reserved for the EAN auto-match — a pre-linked
    push must get its own event type or dashboards counting the EAN case would over-count."""
    channel, profile, source = scaffolding
    sp = SourceProductFactory(
        source=source, external_id="6620", ean=_EAN, data={"weight": "0.150"}, real_product=linked_real_product
    )

    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert not IntegrationEvent.objects.filter(event_type=EventType.AUTO_LINKED_TO_EXISTING_REALPRODUCT.value).exists()
    assert _pre_link_events().count() == 1


def test_linked_sp_records_why_it_skipped_sku_generation(admin_user, scaffolding, linked_real_product):
    channel, profile, source = scaffolding
    sp = SourceProductFactory(
        source=source, external_id="6620", ean=_EAN, data={"weight": "0.150"}, real_product=linked_real_product
    )
    sink: list[dict] = []

    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user, event_sink=sink)

    event = _pre_link_events().get()
    assert event.details["matched_sku"] == linked_real_product.sku
    assert event.details["new_source_idx"] == source.idx
    assert [entry["details"]["matched_via"] for entry in sink] == ["existing_link"]


def test_linked_sp_ignores_a_different_ean_match(admin_user, scaffolding, linked_real_product):
    """The EAN auto-match must not overrule an explicit link — that is how duplicates come back."""
    channel, profile, source = scaffolding
    ean_twin = RealProduct.objects.create(sku="PIM-EAN-TWIN", ean=_EAN, weight=Decimal("0.150"))
    sp = SourceProductFactory(
        source=source, external_id="6620", ean=_EAN, data={"weight": "0.150"}, real_product=linked_real_product
    )

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert product.real_product_id == linked_real_product.id
    assert not Product.objects.filter(real_product=ean_twin).exists()


def test_unlinked_sp_keeps_the_generated_sku_path(admin_user, scaffolding):
    channel, profile, source = scaffolding
    sp = SourceProductFactory(source=source, external_id="6620", data={"weight": "0.150"}, real_product=None)

    product = pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)

    assert product.real_product.sku == pim_writer.generate_sku(source, "6620")
    assert not _pre_link_events().exists()


def test_force_repush_targets_the_linked_realproduct(admin_user, scaffolding, linked_real_product):
    channel, profile, source = scaffolding
    sp = SourceProductFactory(
        source=source, external_id="6620", ean=_EAN, data={"weight": "0.150"}, real_product=linked_real_product
    )
    pim_writer.init_push_to_channel(sp, profile, channel.idx, admin_user)
    sp.refresh_from_db()

    product = pim_writer.force_repush_to_channel(sp, profile, channel.idx, admin_user)

    assert product.real_product_id == linked_real_product.id
    assert RealProduct.objects.count() == 1
    assert Product.objects.count() == 1
