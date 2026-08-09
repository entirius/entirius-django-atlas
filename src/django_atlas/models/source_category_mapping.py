# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class SourceCategoryMapping(BaseModel):
    profile = models.ForeignKey(
        "django_atlas.SourceMappingProfile", on_delete=models.CASCADE, related_name="category_mappings"
    )
    source_field = models.CharField(max_length=128)
    source_value = models.CharField(max_length=512)
    target_category_idx = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "source_field", "source_value"], name="uq_sourcecategorymapping_profile_source_value"
            )
        ]

    def __str__(self) -> str:
        return f"{self.profile_id}:{self.source_field}={self.source_value}->{self.target_category_idx}"
