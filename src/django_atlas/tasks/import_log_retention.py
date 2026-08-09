# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery task: prune ImportLog rows older than retention window."""

from celery import shared_task

from django_atlas.models import SourceSettings
from django_atlas.services import log_service
from django_atlas.settings import QUEUE_DEFAULT


@shared_task(name="django_atlas.prune_import_logs", queue=QUEUE_DEFAULT)
def prune_import_logs_task() -> dict:
    settings = SourceSettings.load()
    days = settings.import_log_retention_days
    deleted = log_service.prune_older_than(days)
    return {"deleted": deleted, "retention_days": days}
