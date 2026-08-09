# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery beat task: dispatch active SourceFeeds whose `schedule_cron` matches now.

`SourceFeed.schedule_cron` previously had no consumer. This task runs every minute
(the host service registers it in CELERY_BEAT_SCHEDULE) and enqueues `execute_feed_task` for every feed whose cron matches the current UTC minute.

Deliberately NOT `is_due(last_run_at)` — `SourceFeed.last_sync_at` only updates after
`finalize_import_log`, so a long-running feed would be re-enqueued on every beat tick until
it finishes (double-dispatch). Matching the current minute against the parsed crontab's field
sets is idempotent per-minute instead: a feed fires at most once per matching minute regardless
of how long the previous run took.
"""

import logging

from celery import shared_task
from celery.schedules import ParseException, crontab
from django.utils import timezone

from django_atlas.models import SourceFeed, SourceSettings
from django_atlas.settings import QUEUE_DEFAULT
from django_atlas.tasks.feed_execution import execute_feed_task

logger = logging.getLogger(__name__)


def _parse_cron_fields(schedule_cron: str) -> tuple[str, str, str, str, str]:
    parts = schedule_cron.split()
    if len(parts) != 5:
        raise ValueError(f"schedule_cron must have 5 fields (m h dom mon dow), got: {schedule_cron!r}")
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return minute, hour, day_of_month, month_of_year, day_of_week


def _is_due_now(schedule_cron: str) -> bool:
    minute, hour, day_of_month, month_of_year, day_of_week = _parse_cron_fields(schedule_cron)
    ct = crontab(
        minute=minute, hour=hour, day_of_month=day_of_month, month_of_year=month_of_year, day_of_week=day_of_week
    )
    now = timezone.now()
    return (
        now.minute in ct.minute
        and now.hour in ct.hour
        and now.day in ct.day_of_month
        and now.month in ct.month_of_year
        and (now.isoweekday() % 7) in ct.day_of_week
    )


@shared_task(name="django_atlas.dispatch_scheduled_feeds", queue=QUEUE_DEFAULT)
def dispatch_scheduled_feeds_task() -> dict:
    settings = SourceSettings.load()
    if not settings.feed_scheduling_enabled:
        return {"dispatched": 0, "skipped_reason": "feed_scheduling_disabled"}

    dispatched = 0
    qs = (
        SourceFeed.objects.filter(is_active=True, source__is_active=True)
        .exclude(schedule_cron="")
        .select_related("source")
    )
    for feed in qs:
        try:
            due = _is_due_now(feed.schedule_cron)
        except (ValueError, ParseException):
            logger.warning("Invalid schedule_cron on feed id=%s: %r", feed.id, feed.schedule_cron)
            continue
        if not due:
            continue
        execute_feed_task.delay(feed.id, mode=feed.sync_mode)
        dispatched += 1
    return {"dispatched": dispatched}
