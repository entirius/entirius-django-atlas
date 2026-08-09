# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the find_duplicate_realproducts management command."""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django_pim.models.real_product import RealProduct

from django_atlas.models import SourceProductLink
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def _run(*args, **kwargs) -> str:
    stdout = StringIO()
    call_command("find_duplicate_realproducts", *args, stdout=stdout, **kwargs)
    return stdout.getvalue()


def test_reports_no_duplicates_when_eans_are_unique():
    RealProduct.objects.create(sku="UNI-001", ean="5900000000001", weight=Decimal("0.150"))
    RealProduct.objects.create(sku="UNI-002", ean="5900000000002", weight=Decimal("0.200"))
    output = _run("--by", "ean")
    assert "No EAN-duplicate groups found." in output


def test_groups_realproducts_sharing_an_ean_and_suggests_merge():
    """Two RealProducts share an EAN and have similar weight → MERGE suggestion."""
    ean = "5900000000099"
    sup_a = SourceFactory(idx="dup-sup-a")
    sup_b = SourceFactory(idx="dup-sup-b")
    rp_a = RealProduct.objects.create(sku="DUP-A-01", ean=ean, weight=Decimal("0.150"))
    rp_b = RealProduct.objects.create(sku="DUP-B-01", ean=ean, weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku=rp_a.sku, source=sup_a, is_primary=True)
    SourceProductLink.objects.create(real_product_sku=rp_b.sku, source=sup_b, is_primary=False)

    output = _run("--by", "ean")

    assert f"EAN {ean}" in output
    assert "DUP-A-01" in output
    assert "DUP-B-01" in output
    assert "dup-sup-a" in output
    assert "dup-sup-b" in output
    assert "★" in output  # primary marker on sup_a's link
    assert "Suggestion: MERGE" in output


def test_groups_with_large_weight_diff_suggest_review():
    ean = "5900000000088"
    RealProduct.objects.create(sku="REV-A-01", ean=ean, weight=Decimal("0.150"))
    RealProduct.objects.create(sku="REV-B-01", ean=ean, weight=Decimal("5.000"))
    output = _run("--by", "ean")
    assert "Suggestion: REVIEW" in output
    assert "exceeds 10% tolerance" in output


def test_unknown_grouping_key_raises():
    with pytest.raises(CommandError):
        _run("--by", "name")
