# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django_utils.models.base_model import BaseModel


class SourceMappingProfile(BaseModel):
    source = models.ForeignKey("django_atlas.Source", on_delete=models.CASCADE, related_name="mapping_profiles")
    idx = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)

    target_channel_idxs = models.JSONField(default=list, blank=True)
    import_language = models.ForeignKey(
        "django_regional.Language", on_delete=models.SET_NULL, related_name="+", null=True, blank=True
    )
    feature_set_idx = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001 — FK-string reference

    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "idx"], name="uq_sourcemappingprofile_source_idx")]
        indexes = [models.Index(fields=["source", "is_active"])]

    def __str__(self) -> str:
        return f"{self.source.idx}/{self.idx}"
