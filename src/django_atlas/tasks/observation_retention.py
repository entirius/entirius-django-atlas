# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery task: prune Observation rows older than the per-kind retention window."""

from celery import shared_task

from django_atlas.enums import SourceKind
from django_atlas.models import SourceSettings
from django_atlas.services import observation_service
from django_atlas.settings import QUEUE_DEFAULT

_DEFAULT_RETENTION_DAYS = 180


@shared_task(name="django_atlas.prune_observations", queue=QUEUE_DEFAULT)
def prune_observations_task() -> dict:
    settings = SourceSettings.load()
    retention_days = settings.observation_retention_days
    deleted_by_kind = {
        kind: observation_service.prune_older_than(kind, retention_days.get(kind, _DEFAULT_RETENTION_DAYS))
        for kind in SourceKind.values
    }
    return {"deleted": sum(deleted_by_kind.values()), "deleted_by_kind": deleted_by_kind}
