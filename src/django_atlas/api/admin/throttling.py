# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Throttle classes for admin API endpoints.

`BulkHasChangesThrottle` protects the bulk badge lookup
(`/pim-sku/has-changes/?skus=...`) which can be hit aggressively by a PIM list
view paginating through products. 120 req/min/user is generous (one page-load
per second with margin) but caps runaway loops or leaked admin tokens.

Service-level override possible via `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`
with key `atlas_change_log_bulk`. Falls back to 120/min when unset so a
mis-configured service never accidentally disables throttling.
"""

from rest_framework.throttling import UserRateThrottle


class BulkHasChangesThrottle(UserRateThrottle):
    scope = "atlas_change_log_bulk"
    rate = "120/min"

    def get_rate(self) -> str:
        try:
            return super().get_rate()
        except Exception:  # noqa: BLE001 — DRF raises ImproperlyConfigured when scope absent
            return self.rate
