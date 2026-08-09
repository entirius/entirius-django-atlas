# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""response schema for POST /admin/realproducts/merge-by-ean/."""

from pydantic import BaseModel, Field


class MergeByEanResponse(BaseModel):
    winner_sku: str = Field(description="RealProduct.sku kept", examples=["AC-ce5b9e3089ff"])
    loser_sku: str = Field(description="RealProduct.sku deleted", examples=["GX-8a1beaee63fe"])
    links_redirected: int = Field(
        description="SourceProductLink rows updated from loser_sku to winner_sku.", examples=[2]
    )
    source_products_repointed: int = Field(
        description="SourceProduct rows whose real_product FK now points at the winner.", examples=[2]
    )
    audit_id: int | None = Field(
        description="SourceProductChangeLog id (null when no SourceProduct is attached to either side).",
        examples=[1234],
    )
