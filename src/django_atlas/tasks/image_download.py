# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery task: per-channel image download + PIM upload + link.

Decyzja #36: PIM `product_picture_service.upload_picture` MUST be idempotent under
concurrent access (sha1 dedup). Multiple parallel tasks for the same URL (multi-channel
SP) rely on PIM-level dedup to avoid duplicate Picture rows.
"""

import os
from urllib.parse import urlparse

from celery import shared_task
from django.core.files.uploadedfile import SimpleUploadedFile

from django_atlas import settings as source_settings
from django_atlas.enums import EventSeverity, EventType, ProductStatus
from django_atlas.models import SourceProduct
from django_atlas.security import safe_get
from django_atlas.services import event_service
from django_atlas.settings import QUEUE_IMAGES


def _filename_from_url(url: str, fallback: str = "image") -> str:
    path = urlparse(url).path
    name = os.path.basename(path) or fallback
    return name


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=2, acks_late=True, queue=QUEUE_IMAGES)
def download_source_images_task(sp_id: int, channel_idx: str) -> dict:
    """Download every image URL for an SP and link to its Product on a channel.

    Idempotency: PIM service handles sha1 dedup (Decyzja #36). Re-running this task
    for the same (sp, channel) is safe — duplicate links are prevented because the
    same Picture is reused.
    """
    from django_pim.models.product import Product
    from django_pim.services import product_picture_service

    sp = SourceProduct.objects.select_related("real_product").get(id=sp_id)
    if sp.real_product_id is None:
        event_service.record(
            event_type=EventType.IMAGE_FAILED.value,
            severity=EventSeverity.WARNING.value,
            source_product=sp,
            message=f"SP {sp.id} has no real_product (cannot link images)",
            details={"channel_idx": channel_idx},
        )
        return {"success": 0, "failed": 0}

    try:
        Product.objects.get(real_product=sp.real_product, shop__idx=channel_idx)
    except Product.DoesNotExist:
        event_service.record(
            event_type=EventType.IMAGE_FAILED.value,
            severity=EventSeverity.WARNING.value,
            source_product=sp,
            message=f"PIM Product not found for sku={sp.real_product.sku} channel={channel_idx}",
            details={"channel_idx": channel_idx},
        )
        return {"success": 0, "failed": 0}

    success = 0
    failed = 0
    headers = {"User-Agent": source_settings.ATLAS_IMAGE_DOWNLOAD_USER_AGENT}

    cap = source_settings.ATLAS_MAX_IMAGE_BYTES
    for index, url in enumerate(sp.image_urls or []):
        host = urlparse(url).hostname or "<unknown>"
        try:
            body = safe_get(url, timeout=source_settings.ATLAS_IMAGE_DOWNLOAD_TIMEOUT_S, cap=cap, headers=headers)
            file_name = _filename_from_url(url)
            uploaded = SimpleUploadedFile(file_name, body)
            picture = product_picture_service.upload_picture(uploaded)
            role = "main" if index == 0 else "general"
            product_picture_service.link_picture_to_product(
                channel_idx=channel_idx,
                sku=sp.real_product.sku,
                picture_pk=picture.pk,
                picture_role=role,
                position=index,
            )
            success += 1
        except Exception as exc:  # noqa: BLE001 — per-image isolation, continue on failure
            # Sanitize event details: log host + error class only, never raw URL/exception body.
            event_service.record(
                event_type=EventType.IMAGE_FAILED.value,
                severity=EventSeverity.WARNING.value,
                source_product=sp,
                message=f"Image fetch/link failed for host={host} ({exc.__class__.__name__})",
                details={"url_host": host, "channel_idx": channel_idx, "error_class": exc.__class__.__name__},
            )
            failed += 1

    # Mark this channel as image-complete (always — failures still count as "we tried").
    images_complete = list(sp.images_complete_channel_idxs or [])
    if channel_idx not in images_complete:
        images_complete.append(channel_idx)
        sp.images_complete_channel_idxs = images_complete

    pushed = set(sp.pushed_to_channel_idxs or [])
    if pushed and pushed == set(images_complete):
        sp.status = ProductStatus.PUSHED.value

    sp.save(update_fields=["images_complete_channel_idxs", "status", "modified_at"])

    return {"success": success, "failed": failed}
