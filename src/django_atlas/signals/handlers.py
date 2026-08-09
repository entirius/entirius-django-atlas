# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Signal handlers for django_atlas.

Stage 4: image dispatch handler — listens to `source_product_pushed_signal`,
schedules per-channel image download via Celery (`transaction.on_commit` per Decyzja #32).

Stage 6: auto-push handler — listens to `source_products_imported_signal`,
schedules `push_approved_for_source_task` for sources with `review_mode='auto'`
(`transaction.on_commit` per Decyzja #32).
"""

from django.db import transaction

from django_atlas.enums import ReviewMode
from django_atlas.signals.killswitch import is_auto_push_enabled, is_suppressed


def on_source_product_pushed(sender, source_product, real_product_sku, channel_idx, **kwargs):  # noqa: ARG001
    """Dispatch image download task after push commit.

    Decyzja #32: `transaction.on_commit` ensures task is dispatched only after
    the surrounding push transaction COMMITS. Without it, a rollback after dispatch
    would leave the task running against a non-existent Product → image_failed cascade.
    """
    if is_suppressed():
        return
    if not source_product.image_urls:
        return  # status already 'pushed' — nothing to download

    sp_id = source_product.id
    from django_atlas.tasks.image_download import download_source_images_task

    transaction.on_commit(lambda: download_source_images_task.delay(sp_id, channel_idx))


def on_source_products_imported(sender, feed, import_log, **kwargs):  # noqa: ARG001
    """Auto-push handler: enqueue push_approved_for_source_task after import commit.

    Skipped when:
      - signals suppressed (`is_suppressed()` thread-local context)
      - import_log.mode is not 'full' or 'delta' (e.g. 'test' init-feed sample)
      - SourceSettings.auto_push_enabled is False (DB killswitch, cached 60s)
      - source.review_mode != 'auto' (manual review still required)

    Decyzja #32: `transaction.on_commit` ensures the task is dispatched only after
    the import-finalization transaction COMMITS. A rollback (failed ImportLog finalize)
    must NOT spawn an auto-push against a half-applied import.
    """
    if is_suppressed():
        return
    if import_log.mode not in ("full", "delta"):
        return
    if not is_auto_push_enabled():
        return
    source = feed.source
    if source.review_mode != ReviewMode.AUTO.value:
        return
    from django_atlas.services import kind_guard

    # Monitoring sources never push — don't even enqueue (preflight would refuse anyway).
    if kind_guard.is_push_blocked(source):
        return

    source_id = source.id
    from django_atlas.tasks.push_pipeline import push_approved_for_source_task

    transaction.on_commit(lambda: push_approved_for_source_task.delay(source_id=source_id, user_id=None))
