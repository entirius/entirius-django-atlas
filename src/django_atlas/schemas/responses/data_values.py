# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""distinct source values per `source_field` for CategoryMapping source_value picker."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DataValue(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    value: str = Field(description="Distinct value as it appears in SourceProduct.data", examples=["Pozostałe"])
    count: int = Field(description="Number of SourceProducts with this value", examples=[87])


class DataValuesResponse(BaseModel):
    source_field: str = Field(description="The source field this response describes", examples=["category_path"])
    values: list[DataValue] = Field(description="Distinct values ranked by count DESC, then value ASC")
    total_distinct: int | None = Field(
        description="Total distinct count when truncation kicked in; null when no truncation (count not computed)",
        examples=[None],
    )
    truncated: bool = Field(description="True if more distinct values existed than `limit`", examples=[False])
    sample_scope: Literal["all"] = Field(
        description="Currently always 'all' — values are aggregated over the full SourceProduct set", examples=["all"]
    )
