# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key", examples=[1])
    idx: str = Field(description="Stable identifier", examples=["amazon-de"])
    name: str = Field(description="Display name", examples=["Amazon Germany"])
    kind: str = Field(description="Discriminator: procurement / monitoring / enrichment", examples=["procurement"])
    source_type: str = Field(description="Type", examples=["feed"])
    review_mode: str = Field(description="Review workflow", examples=["manual"])
    is_active: bool = Field(description="Active flag", examples=[True])
    is_trusted: bool = Field(description="Generic reliability flag", examples=[True])
    default_language_id: int = Field(description="Language PK", examples=[1])
    default_currency_id: int = Field(description="Currency PK", examples=[1])
    country_id: int | None = Field(description="Country PK", examples=[1])
    currency_id: int | None = Field(description="Market-context Currency PK — null=global source", examples=[None])
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
    # Note: `credentials` deliberately omitted — sensitive data, available only via
    # GET /sources/{idx}/credentials/ (super-user only, audited).
    # primary-only physical writes opt-in flag.
    allow_physical_writes_from_non_primary: bool = Field(
        description=(
            "When True, this source's delta sync may overwrite RealProduct physical fields "
            "(weight, ean, width, height, deep) even if its link is not primary."
        ),
        examples=[False],
    )
    # auto-primary selection knobs (CMS Overview tab).
    primary_strategy: str = Field(description="Auto-primary picker strategy", examples=["lowest_cost_with_stock"])
    primary_switch_cooldown_hours: int = Field(description="Minimum hours between auto-primary switches", examples=[24])
    primary_switch_hysteresis_pct: int = Field(
        description="Cost advantage percent required before switching primary", examples=[2]
    )
    eval_frequency: str = Field(description="Auto-primary cron evaluation frequency", examples=["daily"])
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Last update timestamp")


class SourceCredentialsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    idx: str = Field(description="Stable identifier", examples=["amazon-de"])
    credentials: dict = Field(description="Connector API credentials (sensitive)", examples=[{}])


class SourceListResponse(BaseModel):
    count: int = Field(description="Total count", examples=[12])
    next: str | None = Field(description="Next page URL", examples=[None])
    previous: str | None = Field(description="Previous page URL", examples=[None])
    results: list[SourceResponse] = Field(description="Items")


class SourceDeleteResponse(BaseModel):
    mode: str = Field(description="Delete mode: soft or hard", examples=["soft"])
    source_idx: str = Field(description="Source idx that was processed", examples=["amazon-de"])
    affected_links_count: int | None = Field(None, description="Links count (hard mode only)", examples=[0])
    affected_pushed_skus_count: int | None = Field(
        None, description="Pushed SKUs orphaned (hard mode only)", examples=[0]
    )


class SourceDeleteImpactResponse(BaseModel):
    affected_links_count: int = Field(description="SourceProductLink rows that would be deleted", examples=[3])
    affected_pushed_skus_count: int = Field(description="Number of pushed SKUs that would orphan in PIM", examples=[12])
    affected_pushed_skus_sample: list[str] = Field(description="Up to 10 SKUs sample", examples=[["AMZ-1", "AMZ-2"]])
