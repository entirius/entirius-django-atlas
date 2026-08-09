# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceFeedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    source_id: int = Field(description="Source PK", examples=[1])
    idx: str = Field(description="Feed identifier", examples=["main-xml"])
    connector_kind: str = Field(description="Connector entry-point key", examples=["xml_feed"])
    feed_config: dict = Field(description="Connector configuration", examples=[{"url": "https://..."}])
    schedule_cron: str = Field(description="Cron schedule", examples=[""])
    sync_mode: str = Field(description="Sync mode", examples=["full"])
    feature_set_idx: str | None = Field(description="FeatureSet override", examples=[None])
    is_active: bool = Field(description="Active flag", examples=[True])
    language_id: int | None = Field(description="Language PK override", examples=[None])
    currency_id: int | None = Field(description="Currency PK override", examples=[None])
    last_sync_at: datetime | None = Field(description="Last sync timestamp", examples=[None])
    last_sync_status: str = Field(description="Last sync status string", examples=[""])
    last_run_id: UUID | None = Field(description="Last run id (UUID, serialized to string)", examples=[None])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class SourceFeedListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[SourceFeedResponse] = Field(description="Items")


class FeedTriggerResponse(BaseModel):
    run_id: str = Field(description="ImportLog run_id (UUID string)", examples=["3f5b..."])
    status: str = Field(description="ImportLog status after dispatch", examples=["success"])


class FeedTestResponse(BaseModel):
    """Heterogeneous: sync connectors return raw products list, async return dispatch dict."""

    is_async: bool = Field(description="Whether the connector is async", examples=[False])
    raw_products: list[dict[str, Any]] | None = Field(
        None, description="Sync connector sample (list of raw products)", examples=[[]]
    )
    status: str | None = Field(None, description="Async dispatch status (dispatched/suppressed)", examples=[None])
    task_id: str | None = Field(None, description="Async Celery task id", examples=[None])
    run_id: str | None = Field(None, description="Async run id", examples=[None])
    reason: str | None = Field(None, description="Reason if suppressed", examples=[None])
