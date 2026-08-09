# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Request schemas for `/pim-sku/...` admin endpoints."""

from pydantic import BaseModel, Field, model_validator


class AcknowledgeRequest(BaseModel):
    change_ids: list[int] | None = Field(
        default=None, description="Explicit list of audit row PKs to acknowledge. Mutually exclusive with all_unseen."
    )
    all_unseen: bool = Field(default=False, description="If true, acknowledge every unseen audit row for this SKU.")

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "AcknowledgeRequest":
        has_ids = self.change_ids is not None
        if has_ids and self.all_unseen:
            raise ValueError("Specify exactly one of change_ids or all_unseen, not both.")
        if not has_ids and not self.all_unseen:
            raise ValueError("Specify exactly one of change_ids or all_unseen.")
        if has_ids and len(self.change_ids) == 0:
            raise ValueError("change_ids must not be empty (use all_unseen=true instead).")
        return self
