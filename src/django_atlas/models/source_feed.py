# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from django.db import models
from django_utils.models.base_model import BaseModel

from django_atlas.enums import SyncMode


class SourceFeed(BaseModel):
    source = models.ForeignKey("django_atlas.Source", on_delete=models.CASCADE, related_name="feeds")
    idx = models.SlugField(max_length=64)
    connector_kind = models.CharField(max_length=64)
    feed_config = models.JSONField(default=dict, blank=True)
    schedule_cron = models.CharField(max_length=64, blank=True, default="")
    sync_mode = models.CharField(max_length=10, choices=SyncMode.choices, default=SyncMode.FULL)

    language = models.ForeignKey(
        "django_regional.Language", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    currency = models.ForeignKey(
        "django_regional.Currency", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    feature_set_idx = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001 — FK-string reference

    is_active = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=20, blank=True, default="")
    last_run_id = models.UUIDField(null=True, blank=True, default=None)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "idx"], name="uq_sourcefeed_source_idx")]
        indexes = [models.Index(fields=["source", "is_active"])]

    def __str__(self) -> str:
        return f"{self.source.idx}:{self.idx}"

    @staticmethod
    def new_run_id() -> uuid.UUID:
        return uuid.uuid4()
