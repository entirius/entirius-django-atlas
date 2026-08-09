# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IntegrationEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    event_type: str = Field(description="Event type code", examples=["push_succeeded"])
    severity: str = Field(description="critical/warning/info", examples=["info"])
    source_id: int | None = Field(description="Source PK", examples=[1])
    feed_id: int | None = Field(description="Feed PK", examples=[None])
    source_product_id: int | None = Field(description="SourceProduct PK", examples=[None])
    message: str = Field(description="Human-readable message", examples=["Pushed SP 42"])
    details: dict = Field(description="Structured details", examples=[{}])
    acknowledged_at: datetime | None = Field(description="Ack timestamp", examples=[None])
    acknowledged_by_id: int | None = Field(description="Ack User PK", examples=[None])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class IntegrationEventListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[IntegrationEventResponse] = Field(description="Items")
