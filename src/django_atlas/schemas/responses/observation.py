# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, Field


class ObservationResponse(BaseModel):
    source_idx: str = Field(description="Owning Source.idx", examples=["demo-competitor-pl"])
    sku: str = Field(description="PIM RealProduct sku", examples=["CASCADE-001"])
    kind: str = Field(description="Discriminator, denormalized from Source.kind", examples=["monitoring"])
    value: dict = Field(
        description="Canonical shape per kind: monitoring={price,currency,stock}, enrichment={signals}",
        examples=[{"price": "18.50", "currency": "PLN", "stock": 5}],
    )
    ts: datetime = Field(description="Observation timestamp (query-relevant, not created_at)")


class ObservationListResponse(BaseModel):
    count: int = Field(description="Total count", examples=[3])
    next: str | None = Field(description="Next page URL", examples=[None])
    previous: str | None = Field(description="Previous page URL", examples=[None])
    results: list[ObservationResponse] = Field(description="Items")
