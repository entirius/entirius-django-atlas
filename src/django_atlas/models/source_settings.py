# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class SourceSettings(BaseModel):
    auto_push_enabled = models.BooleanField(default=True)
    scraper_dispatch_enabled = models.BooleanField(default=True)
    delta_sync_enabled = models.BooleanField(default=True)
    feed_scheduling_enabled = models.BooleanField(
        default=True,
        help_text="Global killswitch for the dispatch_scheduled_feeds beat.",
    )
    integration_event_retention_days = models.PositiveIntegerField(default=90)
    change_log_retention_days = models.PositiveIntegerField(default=90)
    import_log_retention_days = models.PositiveIntegerField(default=90)
    observation_retention_days = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-kind retention window in days, e.g. {'monitoring': 180, 'enrichment': 180}. "
            "Missing kind keys fall back to 180 (see tasks/observation_retention.py)."
        ),
    )

    class Meta:
        verbose_name = "Source Settings"
        verbose_name_plural = "Source Settings"

    def save(self, *args, **kwargs) -> None:
        self.pk = 1
        if self.created_at is None:
            existing = type(self).objects.filter(pk=1).only("created_at").first()
            if existing is not None:
                self.created_at = existing.created_at
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SourceSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Source Settings"
