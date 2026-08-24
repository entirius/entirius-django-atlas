# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field


class SourceProductPartialUpdateRequest(BaseModel):
    feature_set_idx_override: str | None = Field(
        None, description="Per-SP override of resolved FeatureSet", max_length=64
    )


class BulkApproveRequest(BaseModel):
    ids: list[int] = Field(description="SourceProduct PKs to approve", examples=[[1, 2, 3]], min_length=1)


class BulkRejectRequest(BaseModel):
    ids: list[int] = Field(description="SourceProduct PKs to reject", examples=[[1, 2, 3]], min_length=1)


class BulkRequeueRequest(BaseModel):
    ids: list[int] = Field(
        description="SourceProduct PKs to re-queue (rejected -> queued)", examples=[[1, 2, 3]], min_length=1
    )


class LinkToRealProductRequest(BaseModel):
    real_product_sku: str = Field(
        description="SKU of the EXISTING PIM RealProduct this source product is the same product as",
        examples=["AC-abc"],
        min_length=1,
        max_length=255,
    )
