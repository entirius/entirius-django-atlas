# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django_utils.models.base_model import BaseModel

from django_atlas.enums import ProductStatus, SourceKind


class SourceProduct(BaseModel):
    source = models.ForeignKey("django_atlas.Source", on_delete=models.CASCADE, related_name="products")
    feed = models.ForeignKey(
        "django_atlas.SourceFeed", on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )

    external_id = models.CharField(max_length=128, db_index=True)
    external_id_history = models.JSONField(default=list, blank=True)

    name = models.CharField(max_length=512, db_index=True)
    # Semantic fields nullable per kind — populated depending on the linked Source.kind.
    cost = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True, help_text="Procurement: purchase cost."
    )
    observed_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Monitoring: observed competitor price. clean() rejects this when source.kind != monitoring.",
    )
    signals = models.JSONField(
        null=True, blank=True, default=None, help_text="Enrichment: arbitrary signal payload (e.g. Steam metadata)."
    )
    currency = models.CharField(max_length=3, blank=True, default="")
    stock = models.IntegerField(null=True, blank=True)
    ean = models.CharField(max_length=14, blank=True, default="", db_index=True)
    url = models.URLField(max_length=2048, blank=True, default="")

    image_urls = models.JSONField(default=list, blank=True)
    data = models.JSONField(default=dict, blank=True)
    data_hash = models.CharField(max_length=40, blank=True, default="", db_index=True)

    status = models.CharField(max_length=30, choices=ProductStatus.choices, default=ProductStatus.NEW, db_index=True)
    feature_set_idx_override = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001 — FK-string

    real_product = models.ForeignKey(
        "django_pim.RealProduct", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_products"
    )

    pushed_to_channel_idxs = models.JSONField(default=list, blank=True)
    images_complete_channel_idxs = models.JSONField(default=list, blank=True)

    last_synced_at = models.DateTimeField(default=timezone.now)
    data_changed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    physical_changed_at = models.DateTimeField(null=True, blank=True)

    reviewed_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    pushed_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    pushed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="uq_sourceproduct_source_external")
        ]

    def clean(self) -> None:
        super().clean()
        if self.source_id is None:
            return
        if self.observed_price is not None and self.source.kind != SourceKind.MONITORING.value:
            raise ValidationError({"observed_price": "observed_price is only valid for monitoring-kind sources."})
        if self.cost is not None and self.source.kind != SourceKind.PROCUREMENT.value:
            raise ValidationError({"cost": "cost is only valid for procurement-kind sources."})

    def __str__(self) -> str:
        return f"{self.source.idx}/{self.external_id}"
