# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SourceProductLink — multi-source per PIM RealProduct (M2M-style).

Decision #14: M2M-style mapping between PIM RealProduct (by SKU string) and Source,
with `priority` and `is_primary` to support multi-source scenarios.

Decision #25: `is_primary` defaults to False and is operator-controlled — auto-push
NEVER toggles this field (preserves operator intent across re-imports).
"""

from django.db import models
from django_utils.models.base_model import BaseModel


class SourceProductLink(BaseModel):
    real_product_sku = models.CharField(max_length=128, db_index=True)
    source = models.ForeignKey("django_atlas.Source", on_delete=models.CASCADE, related_name="product_links")

    external_id = models.CharField(max_length=128, blank=True, default="")
    priority = models.IntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")

    # auto-primary selection bookkeeping.
    #   `manual_override` is a sticky bit set by force_set_primary; cron skips the
    #     RealProduct as long as ANY link on the RP has manual_override=True. Cleared
    #     in bulk by reset_to_auto.
    #   `preferred_changed_at` records when is_primary last flipped to True; used
    #     by the cooldown guard. Null until first switch.
    manual_override = models.BooleanField(
        default=False, help_text="When True, auto-primary cron skips this link's RealProduct. Cleared by reset-to-auto."
    )
    preferred_changed_at = models.DateTimeField(
        null=True, blank=True, help_text="Timestamp of the last is_primary=True flip. Used for cooldown check."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["real_product_sku", "source"], name="uq_sourceproductlink_sku_source"),
            models.UniqueConstraint(
                fields=["real_product_sku"],
                condition=models.Q(is_primary=True),
                name="uq_sourceproductlink_one_primary_per_sku",
            ),
        ]
        indexes = [
            models.Index(fields=["real_product_sku"]),
            models.Index(fields=["source"]),
            models.Index(fields=["manual_override"]),
        ]

    def __str__(self) -> str:
        return f"SourceProductLink(sku={self.real_product_sku}, source={self.source.idx})"
