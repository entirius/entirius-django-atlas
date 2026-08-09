# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for realproduct_match_service."""

from decimal import Decimal

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.services import realproduct_match_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# find_match_by_ean
# ---------------------------------------------------------------------------


def test_find_match_by_ean_returns_existing_realproduct():
    rp = RealProduct.objects.create(sku="EXISTING-001", ean="5906214804074", weight=Decimal("0.150"))
    sp = SourceProductFactory(ean="5906214804074")
    found = realproduct_match_service.find_match_by_ean(sp, sp.source)
    assert found is not None
    assert found.pk == rp.pk


def test_find_match_by_ean_returns_none_when_no_match():
    RealProduct.objects.create(sku="OTHER-001", ean="0000000000000", weight=Decimal("0.150"))
    sp = SourceProductFactory(ean="5906214804074")
    assert realproduct_match_service.find_match_by_ean(sp, sp.source) is None


def test_find_match_by_ean_returns_none_when_sp_ean_empty():
    RealProduct.objects.create(sku="ANY-001", ean="5906214804074", weight=Decimal("0.150"))
    sp = SourceProductFactory(ean="")
    assert realproduct_match_service.find_match_by_ean(sp, sp.source) is None


def test_find_match_by_ean_respects_disable_flag():
    RealProduct.objects.create(sku="OPT-001", ean="5906214804074", weight=Decimal("0.150"))
    source = SourceFactory(disable_ean_auto_link=True)
    sp = SourceProductFactory(source=source, ean="5906214804074")
    assert realproduct_match_service.find_match_by_ean(sp, source) is None


def test_find_match_by_ean_returns_oldest_on_duplicates():
    # Two RealProducts sharing an EAN — operator can untangle later via management command.
    first = RealProduct.objects.create(sku="FIRST-001", ean="5906214804074", weight=Decimal("0.150"))
    RealProduct.objects.create(sku="SECOND-001", ean="5906214804074", weight=Decimal("0.155"))
    sp = SourceProductFactory(ean="5906214804074")
    found = realproduct_match_service.find_match_by_ean(sp, sp.source)
    assert found.pk == first.pk


# ---------------------------------------------------------------------------
# physical_tolerance_check
# ---------------------------------------------------------------------------


def test_tolerance_passes_when_all_fields_within_threshold():
    source = SourceFactory(realproduct_match_tolerance_pct=10, realproduct_match_strict=False)
    rp = RealProduct.objects.create(
        sku="TOL-PASS-001",
        ean="5901111111111",
        weight=Decimal("0.150"),
        width=Decimal("20.00"),
        height=Decimal("10.00"),
        deep=Decimal("5.00"),
    )
    defaults = {
        "weight": Decimal("0.155"),  # 3.3% diff
        "width": Decimal("20.50"),  # 2.4% diff
        "height": Decimal("10.10"),  # 1.0% diff
        "deep": Decimal("5.05"),  # 1.0% diff
    }
    result = realproduct_match_service.physical_tolerance_check(defaults, rp, source)
    assert result.passed is True
    assert result.failed_fields == []
    assert set(result.diffs_pct) == {"weight", "width", "height", "deep"}


def test_tolerance_fails_when_weight_exceeds_threshold():
    source = SourceFactory(realproduct_match_tolerance_pct=10, realproduct_match_strict=False)
    rp = RealProduct.objects.create(
        sku="TOL-FAIL-001", ean="5901111111112", weight=Decimal("0.150"), width=Decimal("20.00")
    )
    defaults = {"weight": Decimal("2.500"), "width": Decimal("20.50")}  # weight ~94% diff
    result = realproduct_match_service.physical_tolerance_check(defaults, rp, source)
    assert result.passed is False
    assert "weight" in result.failed_fields
    assert "width" not in result.failed_fields


def test_tolerance_strict_fails_when_field_missing_on_either_side():
    source = SourceFactory(realproduct_match_tolerance_pct=10, realproduct_match_strict=True)
    # existing_rp has weight but no width/height/deep
    rp = RealProduct.objects.create(sku="TOL-STRICT-001", ean="5901111111113", weight=Decimal("0.150"))
    defaults = {"weight": Decimal("0.155")}  # only weight defined on SP side too
    result = realproduct_match_service.physical_tolerance_check(defaults, rp, source)
    assert result.passed is False
    assert set(result.failed_fields) == {"width", "height", "deep"}
    assert result.skipped_fields == []


def test_tolerance_non_strict_skips_missing_fields_and_passes():
    source = SourceFactory(realproduct_match_tolerance_pct=10, realproduct_match_strict=False)
    rp = RealProduct.objects.create(sku="TOL-LAX-001", ean="5901111111114", weight=Decimal("0.150"))
    defaults = {"weight": Decimal("0.155")}
    result = realproduct_match_service.physical_tolerance_check(defaults, rp, source)
    assert result.passed is True
    assert result.failed_fields == []
    assert set(result.skipped_fields) == {"width", "height", "deep"}
    assert "weight" in result.diffs_pct
