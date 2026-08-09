# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for services.duplicate_detection_service."""

from decimal import Decimal

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.models import SourceProductLink
from django_atlas.services import duplicate_detection_service
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def test_empty_when_no_ean_duplicates():
    RealProduct.objects.create(sku="UNI-001", ean="5900000000001", weight=Decimal("0.150"))
    RealProduct.objects.create(sku="UNI-002", ean="5900000000002", weight=Decimal("0.200"))

    groups = duplicate_detection_service.find_duplicates_by_ean()
    assert groups == []


def test_returns_group_with_merge_suggestion_when_weights_close():
    ean = "5900000000777"
    sup = SourceFactory(idx="dd-sup-a")
    RealProduct.objects.create(sku="DD-A-01", ean=ean, weight=Decimal("0.150"))
    RealProduct.objects.create(sku="DD-B-01", ean=ean, weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku="DD-A-01", source=sup, is_primary=True)

    groups = duplicate_detection_service.find_duplicates_by_ean(tolerance_pct=10.0)
    assert len(groups) == 1
    group = groups[0]
    assert group.ean == ean
    assert group.suggestion == "merge"
    assert "within 10% tolerance" in group.suggestion_detail
    assert {rp.sku for rp in group.realproducts} == {"DD-A-01", "DD-B-01"}
    snap_a = next(rp for rp in group.realproducts if rp.sku == "DD-A-01")
    assert snap_a.sources == ({"idx": "dd-sup-a", "name": snap_a.sources[0]["name"], "is_primary": True},)


def test_returns_group_with_review_suggestion_when_weight_diff_large():
    ean = "5900000000888"
    RealProduct.objects.create(sku="RV-A-01", ean=ean, weight=Decimal("0.150"))
    RealProduct.objects.create(sku="RV-B-01", ean=ean, weight=Decimal("5.000"))

    groups = duplicate_detection_service.find_duplicates_by_ean(tolerance_pct=10.0)
    assert len(groups) == 1
    assert groups[0].suggestion == "review"
    assert "exceeds 10% tolerance" in groups[0].suggestion_detail


def test_missing_weight_forces_review():
    ean = "5900000000999"
    RealProduct.objects.create(sku="MW-A-01", ean=ean, weight=Decimal("0.150"))
    RealProduct.objects.create(sku="MW-B-01", ean=ean, weight=None)

    groups = duplicate_detection_service.find_duplicates_by_ean(tolerance_pct=10.0)
    assert len(groups) == 1
    assert groups[0].suggestion == "review"
    assert "Missing weight" in groups[0].suggestion_detail


def test_groups_are_sorted_by_ean():
    RealProduct.objects.create(sku="SORT-Z-01", ean="5900000000002", weight=Decimal("0.150"))
    RealProduct.objects.create(sku="SORT-Z-02", ean="5900000000002", weight=Decimal("0.151"))
    RealProduct.objects.create(sku="SORT-A-01", ean="5900000000001", weight=Decimal("0.150"))
    RealProduct.objects.create(sku="SORT-A-02", ean="5900000000001", weight=Decimal("0.151"))

    groups = duplicate_detection_service.find_duplicates_by_ean()
    assert [g.ean for g in groups] == ["5900000000001", "5900000000002"]
