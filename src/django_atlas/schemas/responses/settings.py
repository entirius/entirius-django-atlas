# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    auto_push_enabled: bool = Field(description="Auto-push toggle", examples=[True])
    scraper_dispatch_enabled: bool = Field(description="Scraper dispatch toggle", examples=[True])
    delta_sync_enabled: bool = Field(description="Delta sync downstream writes toggle", examples=[True])
    feed_scheduling_enabled: bool = Field(
        description="Global killswitch for the scheduled-feed dispatcher beat", examples=[True]
    )
    integration_event_retention_days: int = Field(description="Retention days", examples=[90])
    change_log_retention_days: int = Field(
        description="Retention window for SourceProductChangeLog (days)", examples=[90]
    )
    created_at: datetime = Field(description="Creation timestamp")
    modified_at: datetime = Field(description="Update timestamp")
