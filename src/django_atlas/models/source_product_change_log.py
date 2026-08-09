# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-field audit log for SourceProduct mutations and PIM propagation.

Mirror of `IntegrationEvent` infrastructure (flat event bucket) but captures
**before/after per field** so operators can answer "who/what/when changed cost
on this SKU". Foundation for the read API, PIM/Sources panel bridges, race detection, the cost
subscriber, and cross-source automation.

Source whitelist lives in `enums.ChangeLogSource` (9 values, schema-first).
"""

from django.db import models
from django_utils.models.base_model import BaseModel

from django_atlas.enums import ChangeLogSource


class SourceProductChangeLog(BaseModel):
    source_product = models.ForeignKey(
        "django_atlas.SourceProduct", on_delete=models.CASCADE, related_name="change_logs"
    )
    real_product_sku = models.CharField(max_length=64, db_index=True, blank=True)
    source = models.CharField(max_length=32, choices=ChangeLogSource.choices, db_index=True)
    field_path = models.CharField(max_length=128, db_index=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    triggered_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    applied_to_pim = models.BooleanField(default=False)
    applied_to_pim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["source_product", "-created_at"], name="atlas_changelog_sp_created_idx"),
            models.Index(
                fields=["real_product_sku", "applied_to_pim", "-created_at"], name="atlas_changelog_sku_unseen_idx"
            ),
            models.Index(fields=["source", "-created_at"], name="atlas_changelog_source_idx"),
        ]

    def __str__(self) -> str:
        return f"ChangeLog(sp={self.source_product_id} {self.source}:{self.field_path} @ {self.created_at})"
