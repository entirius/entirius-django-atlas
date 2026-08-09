# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MappingProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    source_id: int = Field(description="Source PK", examples=[1])
    idx: str = Field(description="Profile identifier", examples=["eu-default"])
    name: str = Field(description="Display name", examples=["EU default"])
    target_channel_idxs: list[str] = Field(description="PIM Channel idxs", examples=[["de-store"]])
    import_language_id: int | None = Field(description="Override language PK", examples=[None])
    feature_set_idx: str | None = Field(description="Profile FeatureSet idx", examples=[None])
    is_active: bool = Field(description="Active flag", examples=[True])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class MappingProfileListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[MappingProfileResponse] = Field(description="Items")


class AttributeMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key")
    profile_id: int = Field(description="Profile PK")
    source_field: str = Field(description="Source field/token", examples=["color"])
    target_type: str = Field(description="feature/real_product/skip", examples=["feature"])
    target_identifier: str = Field(description="PIM Feature.idx or RealProduct field", examples=["color"])
    is_required: bool = Field(description="Required flag", examples=[False])
    modifier: str = Field(
        default="none",
        description="Optional unit/string normalisation applied to source value before push",
        examples=["none", "grams_to_kg"],
    )
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class AttributeMappingListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[AttributeMappingResponse] = Field(description="Items")


class CategoryMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key")
    profile_id: int = Field(description="Profile PK")
    source_field: str = Field(description="Source field", examples=["category"])
    source_value: str = Field(description="Source value", examples=["Phones"])
    target_category_idx: str = Field(description="PIM ProductCategory.idx", examples=["phones"])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class CategoryMappingListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[CategoryMappingResponse] = Field(description="Items")


MappingWarningCode = Literal[
    "source_value_not_found_in_source_data",
    "type_incompatibility",
    "no_mappings_configured",
    "feature_set_check_skipped",
]


class MappingWarning(BaseModel):
    """structured warning so the CMS can render per-row badges and suggestions."""

    code: MappingWarningCode = Field(description="Stable warning identifier for FE i18n + per-row routing")
    message: str = Field(
        description="Human-readable EN baseline (FE may localize via code)",
        examples=["Source value 'Pozostalee' not found in SourceProduct.category_path"],
    )
    mapping_kind: Literal["category", "attribute", "profile"] = Field(
        description="Which mapping the warning attaches to (profile = profile-level)", examples=["category"]
    )
    mapping_id: int | None = Field(
        description="PK of the CategoryMapping/AttributeMapping; null for profile-level warnings", examples=[7]
    )
    source_field: str | None = Field(description="Source field involved (if applicable)", examples=["category_path"])
    source_value: str | None = Field(
        description="Source value involved (category warnings only)", examples=["Pozostalee"]
    )
    target_identifier: str | None = Field(
        description="Target identifier involved (attribute warnings only)", examples=[None]
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form context: sample_count, failed_pct, suggestion, expected_type, etc.",
        examples=[{"sample_count": 0, "suggestion": "Pozostałe"}],
    )


class MappingValidationResponse(BaseModel):
    ok: bool = Field(description="Whether the profile is push-ready", examples=[True])
    errors: list[str] = Field(description="Blocking errors", examples=[[]])
    warnings: list[MappingWarning] = Field(
        description="Non-blocking warnings — operator should review before push", examples=[[]]
    )
