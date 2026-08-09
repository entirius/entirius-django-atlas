# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Response schemas for `/pim-sku/...` admin endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChangeLogEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Audit log row primary key")
    source: str = Field(description="ChangeLogSource value (full_sync/delta_sync/...)")
    field_path: str = Field(description="Dotted path of the changed field (e.g. 'physical.weight')")
    before: Any | None = Field(description="Previous value (null if creation)")
    after: Any | None = Field(description="New value (null if deletion)")
    created_at: datetime = Field(description="When the change was recorded")
    applied_to_pim: bool = Field(description="Whether the change has been propagated to PIM")
    applied_to_pim_at: datetime | None = Field(description="When applied_to_pim flipped to true")
    triggered_by: str | None = Field(description="Username of the operator that triggered the change, or null")


class SourceMiniResponse(BaseModel):
    idx: str
    name: str


class SkuChangesResponse(BaseModel):
    real_product_sku: str = Field(description="PIM SKU echoed back")
    has_source: bool = Field(description="True if at least one active SourceProductLink exists")
    source_product_id: int | None = Field(description="Representative SP PK (primary source's), null if none")
    source: SourceMiniResponse | None = Field(description="Primary source mini object, null if no active link")
    unseen_count: int = Field(description="Total unseen (applied_to_pim=false) audit rows for this SKU")
    last_change_at: datetime | None = Field(description="created_at of the most recent audit row, null if none")
    changes: list[ChangeLogEntryResponse] = Field(description="Timeline rows, newest first, capped at 100")


class SkuHasChangesEntry(BaseModel):
    has_source: bool
    source_product_id: int | None
    source_idx: str | None
    unseen_count: int
    last_change_at: datetime | None


class BulkHasChangesResponse(BaseModel):
    skus: dict[str, SkuHasChangesEntry] = Field(description="Map keyed by SKU (preserves request order)")


class AcknowledgeResponse(BaseModel):
    sku: str = Field(description="PIM SKU echoed back")
    acknowledged_count: int = Field(description="Audit rows transitioned from unseen to applied_to_pim=true")


class ForceRepushFailedEntry(BaseModel):
    source_idx: str
    sp_id: int | None = None
    reason: str


class ForceRepushBySkuResponse(BaseModel):
    sku: str = Field(description="PIM SKU echoed back")
    processed_sp_ids: list[int] = Field(description="SP PKs successfully force-repushed")
    pushed_channels_count: int = Field(description="Sum of channel pushes across all processed SPs")
    failed: list[ForceRepushFailedEntry] = Field(description="Per-SP failures (empty list on full success)")
