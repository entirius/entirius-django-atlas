# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Literal

from pydantic import BaseModel, Field

AttributeTargetTypeLiteral = Literal["feature", "real_product", "skip"]

# Mirrors enums.MappingValueModifier — kept as Literal so drf-spectacular emits a fixed
# OpenAPI enum and the CMS gets a typed contract. Order matches enums.py exactly.
MappingValueModifierLiteral = Literal[
    "none",
    "grams_to_kg",
    "kg_to_grams",
    "mm_to_cm",
    "cm_to_mm",
    "mm_to_m",
    "currency_minor_to_major",
    "currency_major_to_minor",
    "string_trim",
    "string_lowercase",
    "string_uppercase",
]


class MappingProfileCreateRequest(BaseModel):
    idx: str = Field(description="Stable profile identifier", examples=["eu-default"], min_length=1, max_length=64)
    name: str = Field(description="Display name", examples=["EU default profile"], min_length=1, max_length=128)
    target_channel_idxs: list[str] = Field(
        description="PIM Channel idxs the profile pushes to", examples=[["de-store", "fr-store"]]
    )
    import_language_id: int | None = Field(None, description="Override import language PK", examples=[1])
    feature_set_idx: str | None = Field(None, description="Profile-level FeatureSet override", max_length=64)
    is_active: bool = Field(True, description="Active flag", examples=[True])


class MappingProfileUpdateRequest(BaseModel):
    name: str | None = Field(None, description="Display name", min_length=1, max_length=128)
    target_channel_idxs: list[str] | None = Field(None, description="PIM Channel idxs")
    import_language_id: int | None = Field(None, description="Override import language PK")
    feature_set_idx: str | None = Field(None, description="Profile FeatureSet idx", max_length=64)
    is_active: bool | None = Field(None, description="Active flag")


class AttributeMappingCreateRequest(BaseModel):
    source_field: str = Field(
        description="Source field key (or token)", examples=["color", "__name__"], min_length=1, max_length=128
    )
    target_type: AttributeTargetTypeLiteral = Field(description="Where to write", examples=["feature"])
    target_identifier: str = Field(
        "", description="PIM Feature.idx / RealProduct field name", examples=["color"], max_length=128
    )
    is_required: bool = Field(False, description="Whether mapping is required", examples=[False])
    modifier: MappingValueModifierLiteral = Field(
        "none",
        description="Optional unit/string normalisation applied to source value before push",
        examples=["none", "grams_to_kg"],
    )


class AttributeMappingUpdateRequest(BaseModel):
    source_field: str | None = Field(None, description="Source field", min_length=1, max_length=128)
    target_type: AttributeTargetTypeLiteral | None = Field(None, description="Target type")
    target_identifier: str | None = Field(None, description="Target identifier", max_length=128)
    is_required: bool | None = Field(None, description="Required flag")
    modifier: MappingValueModifierLiteral | None = Field(None, description="Optional unit/string normalisation")


class CategoryMappingCreateRequest(BaseModel):
    source_field: str = Field(description="Source field key", examples=["category"], min_length=1, max_length=128)
    source_value: str = Field(
        description="Source value to match", examples=["Electronics > Phones"], min_length=1, max_length=512
    )
    target_category_idx: str = Field(
        description="PIM ProductCategory.idx target", examples=["phones"], min_length=1, max_length=64
    )


class CategoryMappingUpdateRequest(BaseModel):
    source_field: str | None = Field(None, description="Source field", max_length=128)
    source_value: str | None = Field(None, description="Source value", max_length=512)
    target_category_idx: str | None = Field(None, description="Target category idx", max_length=64)
