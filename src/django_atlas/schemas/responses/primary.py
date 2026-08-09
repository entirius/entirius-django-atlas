# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Response schemas for `/pim-sku/{sku}/set-primary-source/`
and `/pim-sku/{sku}/reset-primary-to-auto/`.

Both endpoints surface `events: list[dict]` so the CMS can render toasts
for emitted IntegrationEvents.
"""

from typing import Any

from pydantic import BaseModel, Field


class SetPrimarySourceResponse(BaseModel):
    real_product_sku: str = Field(description="PIM SKU echoed back.")
    primary_source_idx: str = Field(description="The source that is now primary.")
    previous_primary_source_idx: str | None = Field(
        description="Source that WAS primary before this call. Null when no prior primary."
    )
    manual_override: bool = Field(description="Always true on this endpoint — sticky flag now set.")
    events: list[dict[str, Any]] = Field(
        default_factory=list,
        description="IntegrationEvents emitted during the switch (forced_warning + any skipped subsidiary events).",
    )


class ResetPrimaryToAutoResponse(BaseModel):
    real_product_sku: str = Field(description="PIM SKU echoed back.")
    previous_primary_source_idx: str | None = Field(
        description="Source that WAS primary (and had manual_override=True). Null if no prior primary."
    )
    new_primary_source_idx: str | None = Field(
        description="Source auto-strategy now picks. Null when no candidates with stock."
    )
    switched: bool = Field(description="True when auto-strategy actually flipped the primary link.")
    skip_reason: str = Field(description="PrimarySkipReason value when switched=False; 'none' when switched=True.")
    events: list[dict[str, Any]] = Field(
        default_factory=list, description="IntegrationEvents emitted during the inline re-evaluation."
    )
