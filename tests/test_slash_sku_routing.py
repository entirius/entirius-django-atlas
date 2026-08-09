# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Slash-containing SKU routing (e.g. "1C01/N") for the PIM-SKU bridge endpoints.

<path:sku> matches slashes; every pim-sku sub-route stays resolvable, and the literal
has-changes/ route is not shadowed.
"""

import pytest
from django.urls import resolve

PREFIX = "/api/atlas/v2/admin/pim-sku"
SKUS = ["1C01/N", "2R04/NR", "PLAIN-001"]


@pytest.mark.parametrize("sku", SKUS)
@pytest.mark.parametrize(
    "suffix,expected",
    [
        ("changes/", "admin-pim-sku-changes"),
        ("acknowledge/", "admin-pim-sku-acknowledge"),
        ("force-repush/", "admin-pim-sku-force-repush"),
        ("set-primary-source/", "admin-pim-sku-set-primary-source"),
        ("reset-primary-to-auto/", "admin-pim-sku-reset-primary-to-auto"),
    ],
)
def test_pim_sku_subroutes(sku, suffix, expected):
    match = resolve(f"{PREFIX}/{sku}/{suffix}")
    assert match.url_name == expected
    assert match.kwargs["sku"] == sku


def test_has_changes_literal_not_shadowed():
    match = resolve(f"{PREFIX}/has-changes/")
    assert match.url_name == "admin-pim-sku-has-changes"
