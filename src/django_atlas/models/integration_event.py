# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_atlas.enums import EventSeverity, EventType


class IntegrationEvent(BaseModel):
    event_type = models.CharField(max_length=64, choices=EventType.choices)
    severity = models.CharField(max_length=10, choices=EventSeverity.choices)

    source = models.ForeignKey(
        "django_atlas.Source", on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    feed = models.ForeignKey(
        "django_atlas.SourceFeed", on_delete=models.SET_NULL, null=True, blank=True, related_name="integration_events"
    )
    source_product = models.ForeignKey(
        "django_atlas.SourceProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="integration_events",
    )

    message = models.TextField()
    details = models.JSONField(default=dict, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "severity"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["acknowledged_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} [{self.severity}] @ {self.created_at}"
