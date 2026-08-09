# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceProductLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    real_product_sku: str = Field(description="PIM RealProduct.sku", examples=["AMZ-12345"])
    source_id: int = Field(description="Source PK", examples=[1])
    external_id: str = Field(description="Source external_id", examples=["B07ABCDEF"])
    priority: int = Field(description="Priority", examples=[10])
    is_primary: bool = Field(description="Operator-controlled primary flag", examples=[False])
    is_active: bool = Field(description="Active flag", examples=[True])
    notes: str = Field(description="Notes", examples=[""])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class SourceProductLinkListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[SourceProductLinkResponse] = Field(description="Items")
