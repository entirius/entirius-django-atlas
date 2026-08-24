# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SourceProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    source_id: int = Field(description="Source PK", examples=[1])
    source_idx: str | None = Field(
        default=None,
        description="Source idx (resolved from FK; null only if source row deleted)",
        examples=["globex"],
    )
    source_name: str | None = Field(
        default=None,
        description="Source display name (resolved from FK; null only if source row deleted)",
        examples=["Globex"],
    )
    kind: str | None = Field(
        default=None,
        description="Source kind (procurement/monitoring/enrichment) — CMS gates push actions on non-procurement",
        examples=["procurement"],
    )
    feed_id: int | None = Field(description="Feed PK", examples=[1])
    external_id: str = Field(description="Source external id", examples=["B07ABCDEF"])
    external_id_history: list[str] = Field(description="Previous external ids (FIFO cap=20)", examples=[[]])
    name: str = Field(description="Product name", examples=["Acme Widget"])
    cost: Decimal | None = Field(description="Procurement: purchase cost", examples=["19.99"])
    observed_price: Decimal | None = Field(description="Monitoring: observed competitor price", examples=[None])
    signals: dict | None = Field(description="Enrichment: arbitrary signal payload", examples=[None])
    currency: str = Field(description="ISO currency code", examples=["EUR"])
    stock: int | None = Field(description="Stock quantity", examples=[42])
    ean: str = Field(description="EAN", examples=["1234567890123"])
    url: str = Field(description="Source URL", examples=["https://source.example/p/B07"])
    image_urls: list[str] = Field(description="Image URLs", examples=[[]])
    data: dict = Field(description="Raw / mapped attribute payload", examples=[{}])
    data_hash: str = Field(description="SHA1 of data payload", examples=[""])
    status: str = Field(description="Review status", examples=["new"])
    feature_set_idx_override: str | None = Field(description="Per-SP override of FeatureSet", examples=[None])
    real_product_id: int | None = Field(description="Linked PIM RealProduct PK", examples=[None])
    real_product_sku: str | None = Field(
        default=None, description="Linked PIM RealProduct SKU (resolved from FK; null when not linked)", examples=[None]
    )
    pushed_to_channel_idxs: list[str] = Field(description="Channel idxs where pushed", examples=[[]])
    images_complete_channel_idxs: list[str] = Field(
        description="Channel idxs with completed image upload", examples=[[]]
    )
    last_synced_at: datetime = Field(description="Last sync timestamp")
    data_changed_at: datetime | None = Field(description="When attribute payload last changed", examples=[None])
    physical_changed_at: datetime | None = Field(description="When physical attributes changed", examples=[None])
    reviewed_by_id: int | None = Field(description="Reviewer User PK", examples=[None])
    reviewed_at: datetime | None = Field(description="When reviewed", examples=[None])
    pushed_by_id: int | None = Field(description="Pusher User PK", examples=[None])
    pushed_at: datetime | None = Field(description="When pushed", examples=[None])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")


class SourceProductListResponse(BaseModel):
    count: int = Field(description="Total count")
    next: str | None = Field(description="Next page URL")
    previous: str | None = Field(description="Previous page URL")
    results: list[SourceProductResponse] = Field(description="Items")


class BulkActionResponse(BaseModel):
    success: int = Field(description="Successfully processed count", examples=[3])
    invalid_transition: int = Field(description="Skipped due to invalid transition", examples=[0])
    ids_failed: list[int] = Field(description="PKs that failed", examples=[[]])


class PushResponse(BaseModel):
    pushed_channels_count: int = Field(description="Number of channels pushed in this call", examples=[2])
    status: str = Field(description="SP status after push", examples=["pushed_pending_images"])
    events: list[dict] = Field(
        default_factory=list,
        description=(
            "Warning events fired during this push. Each entry: "
            "{event_type, severity, message, details}. Empty list when no warnings."
        ),
        examples=[
            [
                {
                    "event_type": "language_fallback",
                    "severity": "warning",
                    "message": "Language fallback: ...",
                    "details": {"profile_idx": "default", "channel_idx": "default-local"},
                }
            ]
        ],
    )


class BulkPushResponse(BaseModel):
    sources_processed: int = Field(description="Sources processed", examples=[1])
    success: int = Field(description="Total successful push count", examples=[5])
    failed: int = Field(description="Total failed push count", examples=[0])
    preflight_failed: list[str] = Field(description="Sources whose preflight failed", examples=[[]])


class UnlinkFromRealProductResponse(BaseModel):
    """Response for operator force-unlink of an auto-linked SP.

    `previous_real_product_sku` is the SKU the SP was attached to before the unlink;
    `new_real_product_sku` is the freshly created RealProduct (per-source-prefixed)
    that now hosts the SP. The original RealProduct is left intact — other linked
    sources may still reference it.
    """

    previous_real_product_sku: str = Field(description="SKU the SP was linked to before unlink", examples=["AC-abc"])
    new_real_product_sku: str = Field(description="SKU of the freshly created RealProduct", examples=["GX-def"])
    events: list[dict] = Field(
        default_factory=list,
        description="Warning events fired during this call. Same shape as PushResponse.events.",
        examples=[[]],
    )


class LinkToRealProductResponse(BaseModel):
    """Response for the operator link of an unlinked SP to an existing RealProduct."""

    real_product_sku: str = Field(description="SKU the SP is now linked to", examples=["AC-abc"])
    link_pk: int = Field(description="PK of the SourceProductLink covering (sku, source)", examples=[7])
    events: list[dict] = Field(
        default_factory=list,
        description="Events fired during this call. Same shape as PushResponse.events.",
        examples=[[]],
    )
