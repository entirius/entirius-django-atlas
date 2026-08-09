# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RawProduct(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_id: str = Field(description="Source-side product identifier", examples=["SKU-001"])
    name: str = Field(description="Product name from feed", examples=["Office Chair"])
    cost: Decimal | None = Field(default=None, description="Net or gross cost from source", examples=["129.00"])
    currency: str | None = Field(default=None, description="ISO 4217 currency code", examples=["EUR"])
    stock: int | None = Field(default=None, description="Available stock quantity", examples=[42])
    ean: str | None = Field(default=None, description="EAN/GTIN code", examples=["5901234123457"])
    url: str | None = Field(default=None, description="Public source URL for the product")
    images: list[str] = Field(default_factory=list, description="Image URLs in source order")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Free-form payload for mapping")


class PriceStockUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_id: str = Field(description="Source-side product identifier", examples=["SKU-001"])
    cost: Decimal | None = Field(default=None, description="Updated cost", examples=["119.00"])
    currency: str | None = Field(default=None, description="ISO 4217 currency code", examples=["EUR"])
    stock: int | None = Field(default=None, description="Updated stock quantity", examples=[10])
    physical: dict[str, Any] | None = Field(
        default=None,
        description="Physical attributes update (weight, ean, width, height, deep)",
        examples=[{"weight": "1.20", "ean": "5901234123457"}],
    )
