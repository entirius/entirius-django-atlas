# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Competitor facade (monitoring Source projection).

Deliberately omits every procurement-only Source field (sku_prefix,
target_warehouse_code, realproduct_match_*, primary_*, qty_*, lead_time_days,
allow_physical_writes_from_non_primary, disable_ean_auto_link) — a human sees
"Competitor", never "monitoring supplier" (architecture-notes §5 Element 1b).
`kind` is forced to monitoring by the view, never accepted from the client.
"""

from pydantic import BaseModel, Field

from django_atlas.schemas.requests.source import ReviewModeLiteral, SourceTypeLiteral


class CompetitorCreateRequest(BaseModel):
    idx: str = Field(
        description="Stable source identifier (slug)", examples=["price-watch-pl"], min_length=1, max_length=64
    )
    name: str = Field(description="Display name in CMS", examples=["PriceWatch Poland"], min_length=1, max_length=128)
    default_language_id: int = Field(description="django_regional.Language PK", examples=[1])
    default_currency_id: int = Field(description="django_regional.Currency PK", examples=[1])
    source_type: SourceTypeLiteral = Field(
        "feed", description="Type; MVP supports 'feed' and 'manual'", examples=["feed"]
    )
    review_mode: ReviewModeLiteral = Field("manual", description="Review workflow", examples=["manual"])
    is_active: bool = Field(True, description="Whether source is active", examples=[True])
    is_trusted: bool = Field(True, description="Generic reliability flag", examples=[True])
    country_id: int | None = Field(None, description="django_regional.Country PK — market context", examples=[1])
    currency_id: int | None = Field(
        None, description="django_regional.Currency PK — market context, null=global", examples=[None]
    )
    company_name: str = Field("", description="Legal company name", examples=[""], max_length=128)
    contact_email: str = Field("", description="Contact email", examples=[""], max_length=254)
    contact_phone: str = Field("", description="Contact phone", examples=[""], max_length=32)
    contact_person: str = Field("", description="Contact person", examples=[""], max_length=128)
    notes: str = Field("", description="Free-form notes", examples=[""])


class CompetitorUpdateRequest(BaseModel):
    name: str | None = Field(None, description="Display name", examples=["PriceWatch PL"], min_length=1, max_length=128)
    default_language_id: int | None = Field(None, description="Language PK", examples=[1])
    default_currency_id: int | None = Field(None, description="Currency PK", examples=[1])
    source_type: SourceTypeLiteral | None = Field(None, description="Type", examples=["feed"])
    review_mode: ReviewModeLiteral | None = Field(None, description="Review workflow", examples=["manual"])
    is_active: bool | None = Field(None, description="Active flag", examples=[True])
    is_trusted: bool | None = Field(None, description="Generic reliability flag", examples=[True])
    country_id: int | None = Field(None, description="Country PK — market context", examples=[1])
    currency_id: int | None = Field(None, description="Currency PK — market context, null=global", examples=[None])
    company_name: str | None = Field(None, description="Company name", max_length=128)
    contact_email: str | None = Field(None, description="Contact email", max_length=254)
    contact_phone: str | None = Field(None, description="Contact phone", max_length=32)
    contact_person: str | None = Field(None, description="Contact person", max_length=128)
    notes: str | None = Field(None, description="Free-form notes")
