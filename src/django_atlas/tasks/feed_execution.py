# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from celery import shared_task

from django_atlas.enums import LogStatus
from django_atlas.models import SourceFeed
from django_atlas.services import data_inspector_service, import_service
from django_atlas.settings import QUEUE_DEFAULT


@shared_task(
    bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3, acks_late=True, queue=QUEUE_DEFAULT
)
def execute_feed_task(self, feed_id: int, mode: str | None = None) -> str:
    feed = SourceFeed.objects.get(pk=feed_id)
    log = import_service.execute_feed(feed, mode=mode or "full", source="scheduler")
    if getattr(log, "status", None) in (LogStatus.SUCCESS.value, LogStatus.PARTIAL.value):
        data_inspector_service.invalidate(feed.source.idx)
    return str(log.run_id)
