# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for validators.validate_routable_sku."""

import pytest

from django_atlas.validators import validate_routable_sku


@pytest.mark.parametrize(
    "sku",
    [
        "AB/changes",
        "AB/pictures",
        "AB/set-primary-source",
        "AB/reset-primary-to-auto",
        "AB/links/1",
        "bulk",
    ],
)
def test_validate_routable_sku_rejects_reserved(sku):
    with pytest.raises(ValueError, match="reserved"):
        validate_routable_sku(sku)


@pytest.mark.parametrize("sku", ["AB-1234", "AB/pictures/CD", "1C01/N", "PLAIN-001"])
def test_validate_routable_sku_accepts_safe(sku):
    validate_routable_sku(sku)  # must not raise
