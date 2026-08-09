# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from django_atlas.enums import LogStatus
from django_atlas.models import ImportLog, SourceFeed

_ERROR_SUMMARY_CAP = 50


def list_logs(
    *,
    feed_id: int | None = None,
    status: str | None = None,
    mode: str | None = None,
    started_at_after: datetime | None = None,
    started_at_before: datetime | None = None,
) -> QuerySet[ImportLog]:
    qs = ImportLog.objects.all()
    if feed_id is not None:
        qs = qs.filter(feed_id=feed_id)
    if status is not None:
        qs = qs.filter(status=status)
    if mode is not None:
        qs = qs.filter(mode=mode)
    if started_at_after is not None:
        qs = qs.filter(started_at__gte=started_at_after)
    if started_at_before is not None:
        qs = qs.filter(started_at__lte=started_at_before)
    return qs.order_by("-started_at")


def get_log(pk: int) -> ImportLog:
    try:
        return ImportLog.objects.get(pk=pk)
    except ImportLog.DoesNotExist as exc:
        raise ValueError(f"ImportLog with pk={pk} not found") from exc


def start_import_log(feed: SourceFeed, *, mode: str, triggered_by=None, source: str = "scheduler") -> ImportLog:
    log = ImportLog.objects.create(
        feed=feed,
        mode=mode,
        status=LogStatus.RUNNING.value,
        started_at=timezone.now(),
        triggered_by=triggered_by,
        source=source,
    )
    feed.last_run_id = log.run_id
    feed.last_sync_status = LogStatus.RUNNING.value
    feed.save(update_fields=["last_run_id", "last_sync_status", "modified_at"])
    return log


def finalize_import_log(
    run_id: UUID,
    *,
    status: str,
    total_count: int = 0,
    new_count: int = 0,
    updated_count: int = 0,
    unchanged_count: int = 0,
    delisted_count: int = 0,
    error_count: int = 0,
    pushed_delisted_count: int = 0,
    mass_delisting_triggered: bool = False,
    physical_updated_count: int = 0,
    physical_skipped_non_primary_count: int = 0,
    physical_overwrite_count: int = 0,
    error_summary: list[Any] | None = None,
) -> ImportLog:
    log = ImportLog.objects.select_related("feed").get(run_id=run_id)
    log.status = status
    log.finished_at = timezone.now()
    log.total_count = total_count
    log.new_count = new_count
    log.updated_count = updated_count
    log.unchanged_count = unchanged_count
    log.delisted_count = delisted_count
    log.error_count = error_count
    log.pushed_delisted_count = pushed_delisted_count
    log.mass_delisting_triggered = mass_delisting_triggered
    #
    log.physical_updated_count = physical_updated_count
    log.physical_skipped_non_primary_count = physical_skipped_non_primary_count
    log.physical_overwrite_count = physical_overwrite_count
    if error_summary is not None:
        log.error_summary = list(error_summary)[:_ERROR_SUMMARY_CAP]
    log.save()

    feed = log.feed
    feed.last_sync_at = log.finished_at
    feed.last_sync_status = status
    feed.save(update_fields=["last_sync_at", "last_sync_status", "modified_at"])
    return log


def prune_older_than(days: int) -> int:
    """Delete ImportLog rows older than `days` (by `started_at`). Returns deleted row count."""
    if days < 0:
        raise ValueError("days must be >= 0.")
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = ImportLog.objects.filter(started_at__lt=cutoff).delete()
    return deleted
