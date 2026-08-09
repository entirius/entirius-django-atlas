# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel

from django_atlas.enums import MappingValueModifier


class AttributeMappingTargetType(models.TextChoices):
    FEATURE = "feature", "Feature"
    REAL_PRODUCT = "real_product", "Real product"
    SKIP = "skip", "Skip"


class SourceAttributeMapping(BaseModel):
    profile = models.ForeignKey(
        "django_atlas.SourceMappingProfile", on_delete=models.CASCADE, related_name="attribute_mappings"
    )
    source_field = models.CharField(max_length=128)
    target_type = models.CharField(max_length=20, choices=AttributeMappingTargetType.choices)
    target_identifier = models.CharField(max_length=128, blank=True, default="")
    is_required = models.BooleanField(default=False)
    modifier = models.CharField(max_length=32, choices=MappingValueModifier.choices, default=MappingValueModifier.NONE)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_field"], name="uq_sourceattributemapping_profile_source")
        ]

    def __str__(self) -> str:
        return f"{self.profile_id}:{self.source_field}->{self.target_type}:{self.target_identifier}"
