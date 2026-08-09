# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""request schema for POST /admin/realproducts/merge-by-ean/."""

from pydantic import BaseModel, Field, field_validator


class MergeByEanRequest(BaseModel):
    winner_sku: str = Field(
        description="RealProduct.sku that keeps all SourceProductLink after the merge.",
        examples=["AC-ce5b9e3089ff"],
        min_length=1,
        max_length=128,
    )
    loser_sku: str = Field(
        description="RealProduct.sku that is deleted; its links are redirected to winner_sku.",
        examples=["GX-8a1beaee63fe"],
        min_length=1,
        max_length=128,
    )
    reason: str = Field(
        description="Operator justification (>=3 chars after trim). Stored on audit log + event.",
        examples=["Same physical product, EAN collision in source feed"],
        min_length=3,
        max_length=512,
    )

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("reason must be at least 3 non-whitespace characters")
        return v
