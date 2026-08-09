# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SourceSettings (singleton) read + update service."""

from typing import Any

from django_atlas.models import SourceSettings

_EDITABLE_FIELDS = frozenset(
    {
        "auto_push_enabled",
        "scraper_dispatch_enabled",
        "delta_sync_enabled",
        "feed_scheduling_enabled",
        "integration_event_retention_days",
        "change_log_retention_days",
    }
)


def get_settings() -> SourceSettings:
    return SourceSettings.load()


def update_settings(**fields: Any) -> SourceSettings:
    invalid = set(fields) - _EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_settings: {sorted(invalid)}")
    settings = SourceSettings.load()
    for field, value in fields.items():
        setattr(settings, field, value)
    if fields:
        settings.save()
    return settings
