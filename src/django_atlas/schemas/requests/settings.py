# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pydantic import BaseModel, Field


class SourceSettingsUpdateRequest(BaseModel):
    auto_push_enabled: bool | None = Field(None, description="Enable auto-push on import", examples=[True])
    scraper_dispatch_enabled: bool | None = Field(None, description="Allow scraper-workers dispatch", examples=[True])
    delta_sync_enabled: bool | None = Field(
        None, description="Allow delta sync downstream writes (QMS/cost)", examples=[True]
    )
    feed_scheduling_enabled: bool | None = Field(
        None, description="Global killswitch for the scheduled-feed dispatcher beat", examples=[True]
    )
    integration_event_retention_days: int | None = Field(
        None, description="Retention window for IntegrationEvent (days)", examples=[90], ge=0
    )
    change_log_retention_days: int | None = Field(
        None, description="Retention window for SourceProductChangeLog (days)", examples=[90], ge=0
    )
