# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Literal

from pydantic import BaseModel, Field

SourceKindLiteral = Literal["procurement", "monitoring", "enrichment"]
SourceTypeLiteral = Literal["feed", "manual", "dropship"]
ReviewModeLiteral = Literal["manual", "auto"]


class SourceCreateRequest(BaseModel):
    idx: str = Field(description="Stable source identifier (slug)", examples=["amazon-de"], min_length=1, max_length=64)
    name: str = Field(description="Display name in CMS", examples=["Amazon Germany"], min_length=1, max_length=128)
    default_language_id: int = Field(description="django_regional.Language PK", examples=[1])
    default_currency_id: int = Field(description="django_regional.Currency PK", examples=[1])
    kind: SourceKindLiteral = Field(
        "procurement", description="Discriminator: procurement / monitoring / enrichment", examples=["procurement"]
    )
    source_type: SourceTypeLiteral = Field(
        "feed", description="Type; MVP supports 'feed' and 'manual'", examples=["feed"]
    )
    review_mode: ReviewModeLiteral = Field("manual", description="Review workflow", examples=["manual"])
    is_active: bool = Field(True, description="Whether source is active", examples=[True])
    is_trusted: bool = Field(True, description="Generic reliability flag", examples=[True])
    country_id: int | None = Field(None, description="django_regional.Country PK", examples=[1])
    currency_id: int | None = Field(
        None,
        description="django_regional.Currency PK — market context for monitoring/enrichment sources, null=global",
        examples=[None],
    )
    sku_prefix: str = Field("", description="Prefix used when generating SKUs", examples=["AMZ"], max_length=10)
    default_feature_set_idx: str | None = Field(
        None, description="PIM FeatureSet.idx default", examples=["consumer-electronics"], max_length=64
    )
    target_warehouse_code: str | None = Field(
        None, description="QMS Warehouse.code", examples=["wh-de-1"], max_length=64
    )
    qty_subtract: int = Field(0, description="Stock buffer subtracted before PIM write", examples=[0], ge=0)
    qty_minimum: int = Field(0, description="Below this stock is treated as zero", examples=[0], ge=0)
    company_name: str = Field("", description="Legal company name", examples=["Acme GmbH"], max_length=128)
    contact_email: str = Field("", description="Contact email", examples=["ops@acme.de"], max_length=254)
    contact_phone: str = Field("", description="Contact phone", examples=["+49 30 1234567"], max_length=32)
    contact_person: str = Field("", description="Contact person", examples=["Jane Doe"], max_length=128)
    notes: str = Field("", description="Free-form notes", examples=["EOM payment terms"])
    lead_time_days: int | None = Field(None, description="Typical lead time in days", examples=[7])


class SourceUpdateRequest(BaseModel):
    name: str | None = Field(None, description="Display name", examples=["Amazon DE"], min_length=1, max_length=128)
    default_language_id: int | None = Field(None, description="Language PK", examples=[1])
    default_currency_id: int | None = Field(None, description="Currency PK", examples=[1])
    kind: SourceKindLiteral | None = Field(None, description="Discriminator", examples=["procurement"])
    source_type: SourceTypeLiteral | None = Field(None, description="Type", examples=["feed"])
    review_mode: ReviewModeLiteral | None = Field(None, description="Review workflow", examples=["manual"])
    is_active: bool | None = Field(None, description="Active flag", examples=[True])
    is_trusted: bool | None = Field(None, description="Generic reliability flag", examples=[True])
    country_id: int | None = Field(None, description="Country PK", examples=[1])
    currency_id: int | None = Field(None, description="Market-context Currency PK, null=global", examples=[None])
    sku_prefix: str | None = Field(None, description="SKU prefix", examples=["AMZ"], max_length=10)
    default_feature_set_idx: str | None = Field(None, description="Default FeatureSet idx", max_length=64)
    target_warehouse_code: str | None = Field(None, description="QMS Warehouse code", max_length=64)
    qty_subtract: int | None = Field(None, description="Stock subtract", ge=0)
    qty_minimum: int | None = Field(None, description="Stock minimum", ge=0)
    company_name: str | None = Field(None, description="Company name", max_length=128)
    contact_email: str | None = Field(None, description="Contact email", max_length=254)
    contact_phone: str | None = Field(None, description="Contact phone", max_length=32)
    contact_person: str | None = Field(None, description="Contact person", max_length=128)
    notes: str | None = Field(None, description="Free-form notes")
    lead_time_days: int | None = Field(None, description="Lead time days")
    # primary-only physical writes opt-in.
    allow_physical_writes_from_non_primary: bool | None = Field(
        None,
        description=(
            "When True, this source's delta sync may overwrite RealProduct physical fields "
            "(weight, ean, width, height, deep) even if its link is not primary. Default False."
        ),
        examples=[False],
    )


class SourceCredentialsUpdateRequest(BaseModel):
    """C1 — write side of the sensitive credentials endpoint (super-user only, audited)."""

    credentials: dict = Field(description="Connector API credentials (sensitive)", examples=[{"api_key": "..."}])
