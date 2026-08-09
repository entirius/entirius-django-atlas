# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""response schemas for GET /admin/auto-matched/."""

from datetime import datetime

from pydantic import BaseModel, Field


class AutoMatchedSource(BaseModel):
    idx: str = Field(description="Source.idx", examples=["acme"])
    name: str = Field(description="Source display name", examples=["Acme Trade Ltd."])
    is_primary: bool = Field(description="True if this link is the primary one for the RealProduct.")


class AutoMatchedRow(BaseModel):
    sku: str = Field(description="RealProduct.sku", examples=["AC-ce5b9e3089ff"])
    ean: str | None = Field(description="EAN/GTIN — null if RealProduct has no EAN.", examples=["5906214804074"])
    sources: list[AutoMatchedSource] = Field(description="All active SourceProductLink for this RealProduct.")
    has_tolerance_violation: bool = Field(
        description="True if a SourceProductChangeLog with source=auto_link was followed by a physical_tolerance_violation event for this SKU."
    )
    has_manual_override: bool = Field(
        description="True if ANY SourceProductLink for this SKU has manual_override=True."
    )
    last_auto_link_at: datetime | None = Field(
        description="Latest SourceProductChangeLog.created_at where source=auto_link for this SKU."
    )


class AutoMatchedListResponse(BaseModel):
    count: int = Field(description="Total matching rows")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[AutoMatchedRow] = Field(description="Page rows")
