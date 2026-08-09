# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field


class SourceProductLinkCreateRequest(BaseModel):
    real_product_sku: str = Field(
        description="PIM RealProduct.sku", examples=["AMZ-12345"], min_length=1, max_length=128
    )
    source_idx: str = Field(description="Source.idx", examples=["amazon-de"], min_length=1, max_length=64)
    external_id: str = Field("", description="Source external_id (optional)", examples=["B07ABCDEF"], max_length=128)
    priority: int = Field(0, description="Priority (higher = primary)", examples=[10])
    is_primary: bool = Field(False, description="Primary flag (operator-controlled)", examples=[False])
    is_active: bool = Field(True, description="Active flag", examples=[True])
    notes: str = Field("", description="Free-form notes")


class SourceProductLinkUpdateRequest(BaseModel):
    external_id: str | None = Field(None, description="External id", max_length=128)
    priority: int | None = Field(None, description="Priority")
    is_primary: bool | None = Field(None, description="Primary flag")
    is_active: bool | None = Field(None, description="Active flag")
    notes: str | None = Field(None, description="Notes")
