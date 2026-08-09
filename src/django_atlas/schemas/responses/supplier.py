# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SupplierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    idx: str = Field(description="Stable identifier", examples=["amazon-de"])
    name: str = Field(description="Display name", examples=["Amazon Germany"])
    source_type: str = Field(description="Type", examples=["feed"])
    review_mode: str = Field(description="Review workflow", examples=["manual"])
    is_active: bool = Field(description="Active flag", examples=[True])
    is_trusted: bool = Field(description="Generic reliability flag", examples=[True])
    default_language_id: int = Field(description="Language PK", examples=[1])
    default_currency_id: int = Field(description="Currency PK", examples=[1])
    country_id: int | None = Field(description="Country PK", examples=[1])
    sku_prefix: str = Field(description="SKU prefix", examples=["AMZ"])
    default_feature_set_idx: str | None = Field(description="Default FeatureSet idx", examples=["consumer-electronics"])
    target_warehouse_code: str | None = Field(description="QMS Warehouse code", examples=["wh-de-1"])
    qty_subtract: int = Field(description="Stock buffer", examples=[0])
    qty_minimum: int = Field(description="Stock minimum", examples=[0])
    company_name: str = Field(description="Company name", examples=["Acme GmbH"])
    contact_email: str = Field(description="Contact email", examples=["ops@acme.de"])
    contact_phone: str = Field(description="Contact phone", examples=["+49..."])
    contact_person: str = Field(description="Contact person", examples=["Jane Doe"])
    notes: str = Field(description="Notes", examples=[""])
    lead_time_days: int | None = Field(description="Lead time days", examples=[7])
    allow_physical_writes_from_non_primary: bool = Field(
        description="Primary-only physical writes opt-in flag", examples=[False]
    )
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Last update timestamp")


class SupplierListResponse(BaseModel):
    count: int = Field(description="Total count", examples=[12])
    next: str | None = Field(description="Next page URL", examples=[None])
    previous: str | None = Field(description="Previous page URL", examples=[None])
    results: list[SupplierResponse] = Field(description="Items")
