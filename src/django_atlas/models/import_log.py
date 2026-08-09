# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import uuid

from django.db import models
from django.utils import timezone
from django_utils.models.base_model import BaseModel

from django_atlas.enums import LogMode, LogSource, LogStatus


class ImportLog(BaseModel):
    feed = models.ForeignKey("django_atlas.SourceFeed", on_delete=models.CASCADE, related_name="import_logs")
    run_id = models.UUIDField(unique=True, default=uuid.uuid4)
    mode = models.CharField(max_length=10, choices=LogMode.choices)
    status = models.CharField(max_length=10, choices=LogStatus.choices, default=LogStatus.RUNNING)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    total_count = models.PositiveIntegerField(default=0)
    new_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    delisted_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    # pushed-product delistings were only emitted as IntegrationEvents,
    # leaving the per-run counter inaccessible to operators inspecting the log directly.
    pushed_delisted_count = models.PositiveIntegerField(default=0)
    mass_delisting_triggered = models.BooleanField(default=False)
    # physical update counters split out from updated_count (which
    # remains cost/qty-only). Operators can dashboard race-skip vs applied vs overwrite.
    physical_updated_count = models.PositiveIntegerField(default=0)
    physical_skipped_non_primary_count = models.PositiveIntegerField(default=0)
    physical_overwrite_count = models.PositiveIntegerField(default=0)

    error_summary = models.JSONField(default=list, blank=True)

    triggered_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    source = models.CharField(max_length=10, choices=LogSource.choices)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"ImportLog({self.feed_id}, {self.run_id}, {self.status})"
