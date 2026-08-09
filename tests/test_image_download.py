# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from unittest.mock import MagicMock, patch

import pytest

from django_atlas.enums import EventType, ProductStatus
from django_atlas.models import IntegrationEvent
from django_atlas.tasks.image_download import _filename_from_url, download_source_images_task
from tests.factories import SourceProductFactory

pytestmark = pytest.mark.django_db


# Helpers ---------------------------------------------------------------


def _build_pushed_sp_with_product(pim_channel_factory, pim_feature_set_factory, channel_idx="ch-img", image_urls=None):
    from django_pim.models.feature_set import FeatureSet
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.real_product import RealProduct

    channel = pim_channel_factory(channel_idx)
    fs, _ = FeatureSet.objects.get_or_create(idx=f"fs-{channel_idx}", defaults={"name": "fs"})
    rp = RealProduct.objects.create(sku=f"sku-{channel_idx}")
    Product.objects.create(
        real_product=rp,
        shop=channel,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    sp = SourceProductFactory(
        external_id=f"ext-{channel_idx}",
        image_urls=list(image_urls or []),
        pushed_to_channel_idxs=[channel_idx],
        status=ProductStatus.PUSHED_PENDING_IMAGES.value,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    return sp, channel, rp


def _mock_response(content: bytes = b"img-bytes", status_code: int = 200):
    response = MagicMock()
    response.content = content
    response.status_code = status_code
    response.iter_content = MagicMock(return_value=[content])
    response.raise_for_status = MagicMock()
    return response


# 1. Happy path -----------------------------------------------------------


def test_happy_path_three_urls_three_pictures(pim_channel_factory, pim_feature_set_factory):
    from django_pim.models.product_picture import ProductPicture

    sp, channel, rp = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-h",
        image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg", "https://example.com/c.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = [_mock_response(b"a-bytes"), _mock_response(b"b-bytes"), _mock_response(b"c-bytes")]
        result = download_source_images_task(sp.id, "ch-img-h")
    assert result == {"success": 3, "failed": 0}
    assert ProductPicture.objects.filter(product__shop=channel, product__real_product=rp).count() == 3


# 2. 404 on one url ------------------------------------------------------


def test_404_skips_one_continues_others(pim_channel_factory, pim_feature_set_factory):
    import requests
    from django_pim.models.product_picture import ProductPicture

    sp, channel, rp = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-404",
        image_urls=["https://example.com/a.jpg", "https://example.com/missing.jpg", "https://example.com/c.jpg"],
    )

    bad = _mock_response()
    bad.raise_for_status.side_effect = requests.HTTPError("404")

    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = [_mock_response(b"a-bytes"), bad, _mock_response(b"c-bytes")]
        result = download_source_images_task(sp.id, "ch-img-404")

    assert result == {"success": 2, "failed": 1}
    assert IntegrationEvent.objects.filter(event_type=EventType.IMAGE_FAILED.value, source_product=sp).count() == 1
    assert ProductPicture.objects.filter(product__shop=channel, product__real_product=rp).count() == 2


# 3. Timeout on one url --------------------------------------------------


def test_timeout_does_not_crash_continues_others(pim_channel_factory, pim_feature_set_factory):
    import requests

    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-to",
        image_urls=["https://example.com/a.jpg", "https://example.com/slow.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = [_mock_response(b"a-bytes"), requests.Timeout("timeout")]
        result = download_source_images_task(sp.id, "ch-img-to")
    assert result["success"] == 1
    assert result["failed"] == 1


# 4. SHA1 dedup (PIM service handles internally) -------------------------


def test_dedup_via_pim_picture_service(pim_channel_factory, pim_feature_set_factory):
    """When two URLs return identical bytes, PIM upload_picture dedupes by sha1.
    Two ProductPicture rows are still created (linking to the same Picture)."""
    from django_pim.models.picture import Picture
    from django_pim.models.product_picture import ProductPicture

    sp, channel, rp = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-dd",
        image_urls=["https://example.com/dup1.jpg", "https://example.com/dup2.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = [_mock_response(b"same-bytes"), _mock_response(b"same-bytes")]
        download_source_images_task(sp.id, "ch-img-dd")
    assert Picture.objects.count() == 1
    assert ProductPicture.objects.filter(product__shop=channel, product__real_product=rp).count() == 2


# 5. Empty image_urls — task is a no-op except marking complete ----------


def test_empty_image_urls_marks_channel_complete(pim_channel_factory, pim_feature_set_factory):
    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory, pim_feature_set_factory, channel_idx="ch-img-empty", image_urls=[]
    )
    result = download_source_images_task(sp.id, "ch-img-empty")
    assert result == {"success": 0, "failed": 0}
    sp.refresh_from_db()
    assert "ch-img-empty" in sp.images_complete_channel_idxs


# 6. Status flip pushed_pending_images → pushed when all channels done ---


def test_status_flips_to_pushed_when_all_channels_complete(pim_channel_factory, pim_feature_set_factory):
    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-flip",
        image_urls=["https://example.com/a.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.return_value = _mock_response(b"x")
        download_source_images_task(sp.id, "ch-img-flip")
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED.value


# 7. Signal triggers task -----------------------------------------------


def test_signal_triggers_task_via_handler(pim_channel_factory, pim_feature_set_factory):
    """The on_source_product_pushed handler should schedule download_source_images_task.delay."""
    from django.db import transaction
    from django.test import TestCase

    from django_atlas.signals import source_product_pushed_signal

    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory, pim_feature_set_factory, channel_idx="ch-img-sig", image_urls=["https://example.com/x.jpg"]
    )
    with patch("django_atlas.tasks.image_download.download_source_images_task.delay") as mdelay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                source_product_pushed_signal.send(
                    sender=type(sp), source_product=sp, real_product_sku="sku", channel_idx="ch-img-sig"
                )
        assert mdelay.call_count == 1


# 8. Killswitch suppress -----------------------------------------------


def test_killswitch_suppress_blocks_dispatch(pim_channel_factory, pim_feature_set_factory):
    from django.db import transaction
    from django.test import TestCase

    from django_atlas.signals import source_product_pushed_signal, suppress_source_signals

    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-kill",
        image_urls=["https://example.com/x.jpg"],
    )
    with patch("django_atlas.tasks.image_download.download_source_images_task.delay") as mdelay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            with transaction.atomic(), suppress_source_signals():
                source_product_pushed_signal.send(
                    sender=type(sp), source_product=sp, real_product_sku="sku", channel_idx="ch-img-kill"
                )
        assert mdelay.call_count == 0


# 9-10. Retry behavior (Celery autoretry) — task itself retries on Exception
# We test the eager raise pattern (Celery would retry asynchronously).


def test_failure_records_event_and_returns_counts(pim_channel_factory, pim_feature_set_factory):
    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-fail",
        image_urls=["https://example.com/x.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = ConnectionError("network down")
        result = download_source_images_task(sp.id, "ch-img-fail")
    assert result["failed"] == 1
    assert IntegrationEvent.objects.filter(event_type=EventType.IMAGE_FAILED.value, source_product=sp).exists()


def test_max_retries_exhausted_event_per_failed_url(pim_channel_factory, pim_feature_set_factory):
    """Each failure URL records one image_failed event regardless of Celery autoretry layer."""
    sp, _, _ = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-3fail",
        image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg", "https://example.com/c.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.side_effect = ConnectionError("down")
        result = download_source_images_task(sp.id, "ch-img-3fail")
    assert result == {"success": 0, "failed": 3}
    assert IntegrationEvent.objects.filter(event_type=EventType.IMAGE_FAILED.value, source_product=sp).count() == 3


# 11. Multi-channel — separate task invocations per channel ------------


def test_multi_channel_status_pending_until_all_complete(pim_channel_factory, pim_feature_set_factory):
    """SP pushed to 2 channels: after channel 1 image task completes, status stays
    pushed_pending_images (channel 2 still pending). After both → pushed."""
    from django_pim.models.feature_set import FeatureSet
    from django_pim.models.product import Product, ProductClassEnum, ProductVisibilityEnum
    from django_pim.models.real_product import RealProduct

    ch1 = pim_channel_factory("ch-img-mc-1")
    ch2 = pim_channel_factory("ch-img-mc-2")
    fs, _ = FeatureSet.objects.get_or_create(idx="fs-mc", defaults={"name": "fs"})
    rp = RealProduct.objects.create(sku="sku-mc")
    Product.objects.create(
        real_product=rp,
        shop=ch1,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    Product.objects.create(
        real_product=rp,
        shop=ch2,
        feature_set=fs,
        is_enabled=False,
        visibility=int(ProductVisibilityEnum.NOT_VISIBLE_INDIVIDUALLY),
        product_class=int(ProductClassEnum.ProductBase),
    )
    sp = SourceProductFactory(
        external_id="mc",
        image_urls=["https://example.com/x.jpg"],
        pushed_to_channel_idxs=["ch-img-mc-1", "ch-img-mc-2"],
        status=ProductStatus.PUSHED_PENDING_IMAGES.value,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])

    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.return_value = _mock_response(b"x")
        download_source_images_task(sp.id, "ch-img-mc-1")
        sp.refresh_from_db()
        assert sp.status == ProductStatus.PUSHED_PENDING_IMAGES.value

        download_source_images_task(sp.id, "ch-img-mc-2")
        sp.refresh_from_db()
        assert sp.status == ProductStatus.PUSHED.value


# 12-13 covered by 11 (transitions checked).


# 14. force re-push wipes images and resets channel completeness -------


def test_force_repush_resets_image_state(
    admin_user, pim_channel_factory, pim_feature_set_factory, pim_typed_feature_factory
):
    """Smoke: after image-complete, force-repush resets the channel from images_complete."""
    from django_pim.models.feature import FeatureTypeEnum
    from django_pim.models.feature_set import FeatureInFeatureSet

    from django_atlas.models import AttributeMappingTargetType
    from django_atlas.services import push_service
    from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceFactory

    ch = pim_channel_factory("ch-img-frp")
    fs = pim_feature_set_factory("fs-img-frp")
    feat = pim_typed_feature_factory("nm-img", int(FeatureTypeEnum.TEXT))
    FeatureInFeatureSet.objects.get_or_create(feature=feat, feature_set=fs)
    sup = SourceFactory(default_feature_set_idx=fs.idx)
    profile = MappingProfileFactory(source=sup, target_channel_idxs=[ch.idx], feature_set_idx=fs.idx)
    AttributeMappingFactory(
        profile=profile,
        source_field="__name__",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=feat.idx,
    )
    sp = SourceProductFactory(
        source=sup,
        status=ProductStatus.APPROVED.value,
        external_id="img-frp-1",
        image_urls=["https://example.com/x.jpg"],
    )
    from django.db import transaction

    with transaction.atomic():
        push_service.push_source_product(sp.id, admin_user)

    # Simulate image task complete for ch-img-frp.
    sp.refresh_from_db()
    sp.images_complete_channel_idxs = [ch.idx]
    sp.status = ProductStatus.PUSHED.value
    sp.save(update_fields=["images_complete_channel_idxs", "status"])

    # Force re-push: channel removed from images_complete (verifies the SP-level reset
    # — actual ProductPicture cleanup tested in pim_writer.force_repush_to_channel
    # which calls ProductPicture.objects.filter(...).delete()).
    push_service.force_repush_source_product(sp.id, admin_user)
    sp.refresh_from_db()
    assert ch.idx not in sp.images_complete_channel_idxs


# 15. Idempotent upload concurrency (sha1 dedup) -----------------------


def test_idempotent_upload_concurrent_same_url(pim_channel_factory, pim_feature_set_factory):
    """2 sequential invocations with identical content → 1 Picture in DB.
    Tests sha1 dedup contract (real concurrency would require threading; this
    tests functional idempotency which is the same behavior under sha1)."""
    from django_pim.models.picture import Picture
    from django_pim.models.product_picture import ProductPicture

    sp, channel, rp = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-img-cc",
        image_urls=["https://example.com/dup.jpg"],
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.return_value = _mock_response(b"identical-bytes")
        download_source_images_task(sp.id, "ch-img-cc")
        # Re-run the SAME task — should hit dedup (1 Picture, 2 ProductPicture links).
        download_source_images_task(sp.id, "ch-img-cc")
    assert Picture.objects.count() == 1
    assert ProductPicture.objects.filter(product__shop=channel, product__real_product=rp).count() == 2


# Helper test: filename derivation -------------------------------------


def test_filename_from_url_uses_basename():
    assert _filename_from_url("https://example.com/path/to/photo.jpg") == "photo.jpg"
    assert _filename_from_url("https://example.com/") == "image"


# Security: SSRF + scheme allowlist + body cap + sanitized event details (C3, C6) ----


def test_ssrf_blocks_file_scheme(pim_channel_factory, pim_feature_set_factory):
    sp, _channel, _rp = _build_pushed_sp_with_product(
        pim_channel_factory, pim_feature_set_factory, channel_idx="ch-ssrf-1", image_urls=["file:///etc/passwd"]
    )
    download_source_images_task(sp.id, "ch-ssrf-1")
    events = IntegrationEvent.objects.filter(source_product=sp, event_type=EventType.IMAGE_FAILED.value)
    assert events.count() == 1
    # Sanitized: details must NOT contain the raw URL or full exception message.
    details = events.first().details
    assert "url" not in details
    assert details.get("url_host") is not None
    assert details.get("error_class") == "ValueError"


def test_ssrf_blocks_internal_ip(pim_channel_factory, pim_feature_set_factory, monkeypatch):
    monkeypatch.setattr("django_atlas.security.url_guard.settings.ATLAS_BLOCK_PRIVATE_HOSTS", True, raising=False)
    # 169.254.169.254 = AWS metadata endpoint (link-local). Must be blocked.
    sp, _channel, _rp = _build_pushed_sp_with_product(
        pim_channel_factory,
        pim_feature_set_factory,
        channel_idx="ch-ssrf-2",
        image_urls=["http://169.254.169.254/latest/meta-data/"],
    )
    download_source_images_task(sp.id, "ch-ssrf-2")
    events = IntegrationEvent.objects.filter(source_product=sp, event_type=EventType.IMAGE_FAILED.value)
    assert events.count() == 1
    assert events.first().details.get("error_class") == "ValueError"


def test_image_body_cap_enforced(pim_channel_factory, pim_feature_set_factory, monkeypatch):
    monkeypatch.setattr("django_atlas.tasks.image_download.source_settings.ATLAS_MAX_IMAGE_BYTES", 100)
    sp, _channel, _rp = _build_pushed_sp_with_product(
        pim_channel_factory, pim_feature_set_factory, channel_idx="ch-cap", image_urls=["https://example.com/big.jpg"]
    )
    with patch("django_atlas.security.url_guard.requests.get") as gget:
        gget.return_value = _mock_response(b"X" * 200)  # 200 bytes > 100 cap
        download_source_images_task(sp.id, "ch-cap")
    events = IntegrationEvent.objects.filter(source_product=sp, event_type=EventType.IMAGE_FAILED.value)
    assert events.count() == 1
    assert events.first().details.get("error_class") == "ValueError"
