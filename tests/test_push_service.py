# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from django_atlas.enums import EventType, ProductStatus
from django_atlas.models import AttributeMappingTargetType, IntegrationEvent, SourceMappingProfile
from django_atlas.services import push_service
from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pushable_source(
    pim_channel_factory,
    pim_feature_set_factory,
    pim_typed_feature_factory,
    *,
    channels=("ch-pf",),
    feature_idx="text-feat",
    feature_set_idx="fs-pf",
    profile_idx="profile-1",
    feature_type=None,
    register_feature=True,
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.feature_set import FeatureInFeatureSet

    if feature_type is None:
        feature_type = int(FeatureTypeEnum.TEXT)

    fs = pim_feature_set_factory(feature_set_idx)
    feat = pim_typed_feature_factory(feature_idx, feature_type)
    if register_feature:
        FeatureInFeatureSet.objects.get_or_create(feature=feat, feature_set=fs)
    for ch in channels:
        pim_channel_factory(ch)
    sup = SourceFactory(default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(
        source=sup, idx=profile_idx, target_channel_idxs=list(channels), feature_set_idx=fs.idx
    )
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    return sup, profile, feat, fs


# ---------------------------------------------------------------------------
# preflight (1-8)
# ---------------------------------------------------------------------------


def test_preflight_happy_path(pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    sup, _, _, _ = _build_pushable_source(pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory)
    result = push_service.preflight_check(sup)
    assert result["ok"] is True


def test_preflight_no_active_profile():
    sup = SourceFactory()
    result = push_service.preflight_check(sup)
    assert result["ok"] is False
    assert any("no_active_mapping_profile" in e for e in result["errors"])


def test_preflight_profile_no_target_channels():
    sup = SourceFactory()
    MappingProfileFactory(source=sup, target_channel_idxs=[])
    result = push_service.preflight_check(sup)
    assert any("profile_no_target_channels" in e for e in result["errors"])


def test_preflight_pim_channel_missing(pim_feature_set_factory, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.feature_set import FeatureInFeatureSet

    fs = pim_feature_set_factory("fs-pre-ch")
    feat = pim_typed_feature_factory("ft", int(FeatureTypeEnum.TEXT))
    FeatureInFeatureSet.objects.get_or_create(feature=feat, feature_set=fs)
    sup = SourceFactory(default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=["ch-ghost"], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    result = push_service.preflight_check(sup)
    assert any("pim_channel_missing" in e for e in result["errors"])


def test_preflight_pim_feature_missing(pim_channel_factory, pim_feature_set_factory):
    fs = pim_feature_set_factory("fs-no-feat")
    pim_channel_factory("ch-no-feat")
    sup = SourceFactory(default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=["ch-no-feat"], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier="ghost-feature",
    )
    result = push_service.preflight_check(sup)
    assert any("pim_feature_missing" in e for e in result["errors"])


def test_preflight_pim_feature_set_missing(pim_channel_factory, pim_typed_feature_factory):
    from django_pim.models.feature import FeatureTypeEnum

    pim_channel_factory("ch-fs-miss")
    feat = pim_typed_feature_factory("ft-fsm", int(FeatureTypeEnum.TEXT))
    sup = SourceFactory(default_feature_set_idx="ghost-fs")
    profile = MappingProfileFactory(source=sup, target_channel_idxs=["ch-fs-miss"], feature_set_idx="ghost-fs")
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    result = push_service.preflight_check(sup)
    assert any("pim_feature_set_missing" in e for e in result["errors"])


def test_preflight_category_missing_all_channels(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from tests.factories import CategoryMappingFactory

    sup, profile, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-cat-1",)
    )
    CategoryMappingFactory(profile=profile, target_category_idx="ghost-cat", source_field="cat", source_value="X")
    result = push_service.preflight_check(sup)
    assert any("pim_category_missing_all_channels" in e for e in result["errors"])


def test_preflight_category_missing_some_channels_warning(
    pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, pim_category_factory
):
    from tests.factories import CategoryMappingFactory

    sup, profile, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-some-1", "ch-some-2")
    )
    ch1 = pim_channel_factory("ch-some-1")
    pim_category_factory(ch1, "cat-here")
    CategoryMappingFactory(profile=profile, target_category_idx="cat-here", source_field="cat", source_value="X")
    result = push_service.preflight_check(sup)
    assert result["ok"] is True
    assert any("pim_category_missing_in_some_channels" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# push_source_product INIT (9-20)
# ---------------------------------------------------------------------------


def test_push_source_product_approved_success(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-ok",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="ok-1")

    with transaction.atomic():
        products = push_service.push_source_product(sp.id, admin_user)
    assert len(products) == 1
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED.value


@pytest.mark.parametrize("status", [ProductStatus.NEW.value, ProductStatus.REJECTED.value, ProductStatus.PUSHED.value])
def test_push_source_product_invalid_status_raises(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, status
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory,
        pim_feature_set_factory,
        pim_typed_feature_factory,
        channels=(f"ch-inv-{status}",),
        feature_idx=f"ft-{status}",
        feature_set_idx=f"fs-{status}",
    )
    sp = SourceProductFactory(source=sup, status=status, external_id=f"inv-{status}")
    with pytest.raises(ValueError, match="expected 'approved'"):
        with transaction.atomic():
            push_service.push_source_product(sp.id, admin_user)


def test_push_source_product_multi_channel_creates_multiple_products(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-mc-1", "ch-mc-2")
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="mc-1")
    with transaction.atomic():
        products = push_service.push_source_product(sp.id, admin_user)
    assert len(products) == 2


def test_push_multi_profile_unions_channels(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.feature_set import FeatureInFeatureSet

    fs = pim_feature_set_factory("fs-mp")
    feat = pim_typed_feature_factory("ft-mp", int(FeatureTypeEnum.TEXT))
    FeatureInFeatureSet.objects.get_or_create(feature=feat, feature_set=fs)
    pim_channel_factory("ch-mp-a")
    pim_channel_factory("ch-mp-b")
    sup = SourceFactory(default_feature_set_idx=fs.idx)
    p1 = MappingProfileFactory(source=sup, idx="p-mp-1", target_channel_idxs=["ch-mp-a"], feature_set_idx=fs.idx)
    p2 = MappingProfileFactory(source=sup, idx="p-mp-2", target_channel_idxs=["ch-mp-b"], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=p1,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    AttributeMappingFactory(
        profile=p2,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="mp-1")

    with transaction.atomic():
        products = push_service.push_source_product(sp.id, admin_user)

    assert len(products) == 2


def test_push_status_pending_images_when_image_urls(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-img",)
    )
    sp = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="img-1", image_urls=["https://example.com/a.jpg"]
    )
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED_PENDING_IMAGES.value


def test_push_status_pushed_when_no_image_urls(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-noimg",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="noimg-1", image_urls=[])
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED.value


def test_push_records_pushed_to_channel_idxs(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-rc-1", "ch-rc-2")
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="rc-1")
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert set(sp.pushed_to_channel_idxs) == {"ch-rc-1", "ch-rc-2"}


def test_push_sets_pushed_by_and_pushed_at(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-by",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="by-1")
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert sp.pushed_by_id == admin_user.id
    assert sp.pushed_at is not None


def test_push_links_real_product_fk_on_sp(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-fk",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="fk-1")
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert sp.real_product_id is not None


def test_push_emits_signal_per_channel(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_atlas.signals import source_product_pushed_signal

    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-sig-1", "ch-sig-2")
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="sig-1")

    received = []

    def _spy(sender, source_product, real_product_sku, channel_idx, **kwargs):
        received.append((real_product_sku, channel_idx))

    source_product_pushed_signal.connect(_spy)
    try:
        with transaction.atomic():
            push_service.push_source_product(sp.id, admin_user)
    finally:
        source_product_pushed_signal.disconnect(_spy)

    channels_seen = {c for _, c in received}
    assert channels_seen == {"ch-sig-1", "ch-sig-2"}


# ---------------------------------------------------------------------------
# push_approved_for_source (21-24)
# ---------------------------------------------------------------------------


def test_push_approved_iterates_only_approved(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-it",)
    )
    for i in range(5):
        SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id=f"ok-{i}")
    for i in range(2):
        SourceProductFactory(source=sup, status=ProductStatus.REJECTED.value, external_id=f"rej-{i}")

    result = push_service.push_approved_for_source(sup.id, user=admin_user)
    assert result["success"] == 5
    assert result["failed"] == 0


def test_push_approved_records_failed_and_continues(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-fc",)
    )
    SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="ok-fc-1")
    bad = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="bad-fc")
    SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="ok-fc-2")

    real_init = push_service.pim_writer.init_push_to_channel

    def _flaky(sp, *args, **kwargs):
        if sp.external_id == "bad-fc":
            raise RuntimeError("boom")
        return real_init(sp, *args, **kwargs)

    with patch.object(push_service.pim_writer, "init_push_to_channel", side_effect=_flaky):
        result = push_service.push_approved_for_source(sup.id, user=admin_user)

    assert result["success"] == 2
    assert result["failed"] == 1
    assert IntegrationEvent.objects.filter(event_type=EventType.PUSH_FAILED.value, source_product=bad).exists()


def test_push_approved_returns_early_on_preflight_failure(admin_user):
    sup = SourceFactory()
    # No active profile → preflight fails
    SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="x")
    result = push_service.push_approved_for_source(sup.id, user=admin_user)
    assert result["preflight_failed"] is True
    assert result["success"] == 0
    assert result["failed"] == 0


def test_push_approved_empty_set(admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-empty",)
    )
    result = push_service.push_approved_for_source(sup.id, user=admin_user)
    assert result == {"success": 0, "failed": 0, "preflight_failed": False, "errors": []}


# ---------------------------------------------------------------------------
# Defensive concurrency (25.0a-25.0d)
# ---------------------------------------------------------------------------


def test_profile_re_read_per_sp_respects_concurrent_deactivation(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """Defensive re-read: between SP push #1 and SP push #2, deactivating the only
    active profile causes preflight to fail on push #2 — proves we re-read state
    from DB rather than reusing a snapshot.
    """
    sup, profile, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-rr",)
    )
    sp1 = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="rr-1")
    sp2 = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="rr-2")

    with transaction.atomic():
        push_service.push_source_product(sp1.id, admin_user)

    SourceMappingProfile.objects.filter(pk=profile.pk).update(is_active=False)

    # 2nd push: preflight fails because no active profile (defensive re-read of state).
    with pytest.raises(ValueError, match="no_active_mapping_profile"):
        with transaction.atomic():
            push_service.push_source_product(sp2.id, admin_user)


def test_idempotent_skip_for_already_pushed_channel(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-idem-1", "ch-idem-2")
    )
    sp = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="idem-1", pushed_to_channel_idxs=["ch-idem-1"]
    )

    real_init = push_service.pim_writer.init_push_to_channel
    calls = []

    def _spy(sp_arg, profile, channel_idx, user, **kwargs):
        calls.append(channel_idx)
        return real_init(sp_arg, profile, channel_idx, user, **kwargs)

    with patch.object(push_service.pim_writer, "init_push_to_channel", side_effect=_spy):
        with transaction.atomic():
            push_service.push_source_product(sp.id, admin_user)

    assert calls == ["ch-idem-2"]


def test_select_for_update_is_used(admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-sfu",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="sfu-1")
    with CaptureQueriesContext(connection) as ctx:
        with transaction.atomic():
            push_service.push_source_product(sp.id, admin_user)
    sql_dump = " ".join(q["sql"] for q in ctx.captured_queries).lower()
    assert "for update" in sql_dump


def test_image_dispatch_is_on_commit(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """image task .delay() runs only after commit; rollback prevents dispatch.

    Uses Django's TestCase.captureOnCommitCallbacks to capture queued callbacks
    without relying on actual transaction commit (pytest.mark.django_db wraps tests
    in a savepoint, so transaction.on_commit never fires naturally).
    """
    from django.test import TestCase

    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-oc",)
    )
    sp = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="oc-1", image_urls=["https://example.com/a.jpg"]
    )

    # Happy path: commit fires on_commit callbacks → delay called.
    with patch("django_atlas.tasks.image_download.download_source_images_task.delay") as mocked_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                push_service.push_source_product(sp.id, admin_user)
        assert mocked_delay.call_count == 1

    # Rollback path: callbacks captured but NOT executed (execute=False),
    # AND the inner atomic block raises so callbacks are discarded.
    sp2 = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="oc-2", image_urls=["https://example.com/b.jpg"]
    )
    with patch("django_atlas.tasks.image_download.download_source_images_task.delay") as mocked_delay2:
        try:
            with TestCase.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    push_service.push_source_product(sp2.id, admin_user)
                    raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        # Rollback discarded the on_commit callbacks → delay NEVER called.
        assert mocked_delay2.call_count == 0


# ---------------------------------------------------------------------------
# force_repush (25-33)
# ---------------------------------------------------------------------------


def _build_pushed_sp(admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=channels
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="frp-base", image_urls=[])
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    return sp


def test_force_repush_from_pushed_succeeds(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sp = _build_pushed_sp(
        admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, ("ch-frp-a",)
    )
    products = push_service.force_repush_source_product(sp.id, admin_user)
    assert len(products) == 1


def test_force_repush_from_pushed_pending_images_succeeds(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-frp-pp",)
    )
    sp = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="frp-pp", image_urls=["https://example.com/x.jpg"]
    )
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED_PENDING_IMAGES.value
    products = push_service.force_repush_source_product(sp.id, admin_user)
    assert len(products) == 1


def test_force_repush_from_approved_raises(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-frp-app",)
    )
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="frp-app")
    with pytest.raises(ValueError, match="force re-push requires"):
        push_service.force_repush_source_product(sp.id, admin_user)


def test_force_repush_does_not_change_is_enabled(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sp = _build_pushed_sp(
        admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, ("ch-frp-en",)
    )
    from django_pim.models.product import Product

    product = Product.objects.get(real_product=sp.real_product, shop__idx="ch-frp-en")
    product.is_enabled = True
    product.save(update_fields=["is_enabled"])

    push_service.force_repush_source_product(sp.id, admin_user)
    product.refresh_from_db()
    assert product.is_enabled is True


def test_force_repush_replaces_attributes(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from django_pim.models.product_attribute import ProductAttribute

    sp = _build_pushed_sp(
        admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, ("ch-frp-attr",)
    )
    # Initial push wrote one attribute; sp.name = "Product frp-base" originally.
    sp.name = "Product (renamed)"
    sp.save(update_fields=["name"])
    push_service.force_repush_source_product(sp.id, admin_user)

    pa = ProductAttribute.objects.get(product__real_product=sp.real_product, product__shop__idx="ch-frp-attr")
    assert pa.value_txt == "Product (renamed)"


def test_force_repush_replaces_categories(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, pim_category_factory
):
    from django_pim.models.product import Product
    from django_pim.models.product_in_category import ProductInCategory

    from tests.factories import CategoryMappingFactory

    sup, profile, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-frp-cat",)
    )
    channel = pim_channel_factory("ch-frp-cat")
    cat = pim_category_factory(channel, "cat-frp")
    CategoryMappingFactory(profile=profile, source_field="cat", source_value="A", target_category_idx="cat-frp")
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, external_id="frp-cat", data={"cat": "A"})
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    product = Product.objects.get(real_product=sp.real_product, shop=channel)
    assert ProductInCategory.objects.filter(product=product, category=cat).count() == 1

    push_service.force_repush_source_product(sp.id, admin_user)
    # Still 1 (full delete + recreate, no duplicates).
    assert ProductInCategory.objects.filter(product=product, category=cat).count() == 1


def test_force_repush_overwrites_real_product_physical(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    from decimal import Decimal

    from tests.factories import AttributeMappingFactory

    sup, profile, _, fs = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-frp-rp",)
    )
    AttributeMappingFactory(
        profile=profile,
        source_field="weight",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
    )
    sp = SourceProductFactory(
        source=sup, status=ProductStatus.APPROVED.value, external_id="frp-rp", data={"weight": "1.50"}
    )
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    sp.data = {"weight": "2.75"}
    sp.save(update_fields=["data"])

    push_service.force_repush_source_product(sp.id, admin_user)
    sp.real_product.refresh_from_db()
    assert sp.real_product.weight == Decimal("2.75")


def test_force_repush_resets_images_complete(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    sup, _, _, _ = _build_pushable_source(
        pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, channels=("ch-frp-img",)
    )
    sp = SourceProductFactory(
        source=sup,
        status=ProductStatus.APPROVED.value,
        external_id="frp-img",
        image_urls=["https://example.com/x.jpg"],
        images_complete_channel_idxs=[],
    )
    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    sp.images_complete_channel_idxs = ["ch-frp-img"]
    sp.save(update_fields=["images_complete_channel_idxs"])

    push_service.force_repush_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert "ch-frp-img" not in sp.images_complete_channel_idxs


def test_force_repush_emits_event(admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory):
    sp = _build_pushed_sp(
        admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory, ("ch-frp-ev",)
    )
    push_service.force_repush_source_product(sp.id, admin_user)
    assert IntegrationEvent.objects.filter(event_type=EventType.FORCE_REPUSH_EXECUTED.value, source_product=sp).exists()
