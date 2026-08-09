# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Literal

from pydantic import BaseModel, Field

SyncModeLiteral = Literal["full", "delta"]


class SourceFeedCreateRequest(BaseModel):
    idx: str = Field(description="Stable feed identifier", examples=["main-xml"], min_length=1, max_length=64)
    connector_kind: str = Field(
        description="Connector entry-point key", examples=["xml_feed"], min_length=1, max_length=64
    )
    feed_config: dict = Field(
        default_factory=dict, description="Connector-specific configuration", examples=[{"url": "https://..."}]
    )
    sync_mode: SyncModeLiteral = Field("full", description="Sync mode", examples=["full"])
    schedule_cron: str = Field(
        "", description="Cron schedule (empty = manual only)", examples=["0 */6 * * *"], max_length=64
    )
    feature_set_idx: str | None = Field(None, description="Override of source default FeatureSet", max_length=64)
    is_active: bool = Field(True, description="Active flag", examples=[True])
    language_id: int | None = Field(None, description="Override language PK", examples=[1])
    currency_id: int | None = Field(None, description="Override currency PK", examples=[1])


class SourceFeedUpdateRequest(BaseModel):
    connector_kind: str | None = Field(None, description="Connector kind", max_length=64)
    feed_config: dict | None = Field(None, description="Connector configuration")
    sync_mode: SyncModeLiteral | None = Field(None, description="Sync mode")
    schedule_cron: str | None = Field(None, description="Cron schedule", max_length=64)
    feature_set_idx: str | None = Field(None, description="Override FeatureSet idx", max_length=64)
    is_active: bool | None = Field(None, description="Active flag")
    language_id: int | None = Field(None, description="Override language PK")
    currency_id: int | None = Field(None, description="Override currency PK")


class FeedTriggerRequest(BaseModel):
    mode: SyncModeLiteral = Field("full", description="Sync mode for this run", examples=["full"])
