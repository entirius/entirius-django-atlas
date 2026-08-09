# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery task: push approved SourceProducts for a source."""

from celery import shared_task
from django.contrib.auth import get_user_model

from django_atlas.services import push_service
from django_atlas.settings import QUEUE_DEFAULT


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3, acks_late=True, queue=QUEUE_DEFAULT)
def push_approved_for_source_task(source_id: int, user_id: int | None = None) -> dict:
    user = None
    if user_id is not None:
        User = get_user_model()
        user = User.objects.filter(id=user_id).first()
    return push_service.push_approved_for_source(source_id, user=user)
