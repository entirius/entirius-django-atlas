# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, ConfigDict, Field


class TokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="Reserved token", examples=["__name__"])
    description: str = Field(description="What the token maps to", examples=["Mapped from SourceProduct.name"])


class DataKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="Dot-path into SourceProduct.data", examples=["category_path"])
    presence_pct: int = Field(description="Percentage of sampled rows containing this key", examples=[98])
    sample_value: str = Field(
        description="First non-null sample value (truncated to 120 chars)", examples=["Gry/Plansze"]
    )
    type: str = Field(description="Leaf type: string / number / bool / array / null / object", examples=["string"])


class DataKeysResponse(BaseModel):
    tokens: list[TokenResponse] = Field(description="Reserved tokens always offered to the operator")
    data_keys: list[DataKeyResponse] = Field(description="Flattened keys observed in SourceProduct.data")
    sample_size: int = Field(description="Actual number of SourceProduct rows sampled", examples=[100])
