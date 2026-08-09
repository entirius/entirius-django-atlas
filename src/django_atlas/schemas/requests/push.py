# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field


class BulkPushRequest(BaseModel):
    source_idx: str | None = Field(
        None, description="Push only this source (default: all active)", examples=["amazon-de"], max_length=64
    )
