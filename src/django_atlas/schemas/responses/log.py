# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ImportLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    feed_id: int = Field(description="Feed PK", examples=[1])
    run_id: UUID = Field(description="Run UUID (serialized to string)", examples=["3f5b..."])
    mode: str = Field(description="full/delta/test", examples=["full"])
    status: str = Field(description="running/success/partial/failed", examples=["success"])
    started_at: datetime = Field(description="Start timestamp")
    finished_at: datetime | None = Field(description="Finish timestamp", examples=[None])
    total_count: int = Field(description="Total products processed", examples=[100])
    new_count: int = Field(description="New products", examples=[5])
    updated_count: int = Field(description="Updated products", examples=[10])
    unchanged_count: int = Field(description="Unchanged products", examples=[80])
    delisted_count: int = Field(
        description="Delisted products (non-pushed: NEW/QUEUED/APPROVED → REJECTED)", examples=[5]
    )
    error_count: int = Field(description="Errors", examples=[0])
    pushed_delisted_count: int = Field(description="Pushed products that disappeared from feed", examples=[0])
    mass_delisting_triggered: bool = Field(
        description="True when >50% of pushed products vanished in this run", examples=[False]
    )
    error_summary: list = Field(description="Error samples (cap 50)", examples=[[]])
    triggered_by_id: int | None = Field(description="Triggering User PK", examples=[None])
    source: str = Field(description="cli/api/scheduler/signal", examples=["api"])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class ImportLogListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[ImportLogResponse] = Field(description="Items")
