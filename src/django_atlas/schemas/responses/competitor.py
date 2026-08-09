# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompetitorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    idx: str = Field(description="Stable identifier", examples=["price-watch-pl"])
    name: str = Field(description="Display name", examples=["PriceWatch Poland"])
    source_type: str = Field(description="Type", examples=["feed"])
    review_mode: str = Field(description="Review workflow", examples=["manual"])
    is_active: bool = Field(description="Active flag", examples=[True])
    is_trusted: bool = Field(description="Generic reliability flag", examples=[True])
    default_language_id: int = Field(description="Language PK", examples=[1])
    default_currency_id: int = Field(description="Currency PK", examples=[1])
    country_id: int | None = Field(description="Country PK — market context", examples=[1])
    currency_id: int | None = Field(description="Currency PK — market context, null=global source", examples=[None])
    company_name: str = Field(description="Company name", examples=[""])
    contact_email: str = Field(description="Contact email", examples=[""])
    contact_phone: str = Field(description="Contact phone", examples=[""])
    contact_person: str = Field(description="Contact person", examples=[""])
    notes: str = Field(description="Notes", examples=[""])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Last update timestamp")


class CompetitorListResponse(BaseModel):
    count: int = Field(description="Total count", examples=[3])
    next: str | None = Field(description="Next page URL", examples=[None])
    previous: str | None = Field(description="Previous page URL", examples=[None])
    results: list[CompetitorResponse] = Field(description="Items")
