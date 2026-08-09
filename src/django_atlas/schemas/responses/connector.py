# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field


class ConnectorResponse(BaseModel):
    kind: str = Field(description="Connector entry-point key", examples=["xml_feed"])
    name: str = Field(description="Display name", examples=["XML Feed"])
    is_async: bool = Field(description="Whether connector is async", examples=[False])


class ConnectorListResponse(BaseModel):
    results: list[ConnectorResponse] = Field(description="Connectors discovered via entry-points")
