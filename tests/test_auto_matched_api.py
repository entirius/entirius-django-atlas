# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for GET /admin/auto-matched/."""

from decimal import Decimal

import pytest
from django.urls import reverse
from django_pim.models.real_product import RealProduct

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType
from django_atlas.models import IntegrationEvent, SourceProductChangeLog, SourceProductLink
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _seed_auto_linked(*, sku: str, source_idx: str, ean: str = "5900000123456", manual_override: bool = False):
    sup = SourceFactory(idx=source_idx)
    rp = RealProduct.objects.create(sku=sku, ean=ean, weight=Decimal("0.150"))
    sp = SourceProductFactory(source=sup, real_product=rp)
    SourceProductLink.objects.create(real_product_sku=sku, source=sup, is_primary=True, manual_override=manual_override)
    SourceProductChangeLog.objects.create(
        source_product=sp, real_product_sku=sku, source=ChangeLogSource.AUTO_LINK.value, field_path="real_product.link"
    )
    return sup, rp, sp


def test_auto_matched_returns_seeded_row(admin_client):
    _seed_auto_linked(sku="AM-001", source_idx="am-sup")
    response = admin_client.get(reverse("admin-auto-matched-list"))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] >= 1
    skus = {row["sku"] for row in body["results"]}
    assert "AM-001" in skus
    row = next(r for r in body["results"] if r["sku"] == "AM-001")
    assert row["ean"] == "5900000123456"
    assert any(s["idx"] == "am-sup" for s in row["sources"])
    assert row["has_manual_override"] is False
    assert row["has_tolerance_violation"] is False


def test_auto_matched_filters_by_source(admin_client):
    _seed_auto_linked(sku="AM-A-01", source_idx="am-a", ean="5900000000001")
    _seed_auto_linked(sku="AM-B-01", source_idx="am-b", ean="5900000000002")

    response = admin_client.get(reverse("admin-auto-matched-list"), {"source": "am-a"})
    assert response.status_code == 200
    skus = {row["sku"] for row in response.json()["results"]}
    assert "AM-A-01" in skus
    assert "AM-B-01" not in skus


def test_auto_matched_filters_manual_override_only(admin_client):
    _seed_auto_linked(sku="AM-MO-01", source_idx="am-mo-a", ean="5900000003333", manual_override=True)
    _seed_auto_linked(sku="AM-AUTO-01", source_idx="am-mo-b", ean="5900000004444", manual_override=False)

    response = admin_client.get(reverse("admin-auto-matched-list"), {"manual_override_only": "true"})
    assert response.status_code == 200
    skus = {row["sku"] for row in response.json()["results"]}
    assert "AM-MO-01" in skus
    assert "AM-AUTO-01" not in skus


def test_auto_matched_flags_tolerance_violations(admin_client):
    sup, rp, sp = _seed_auto_linked(sku="AM-VIO-01", source_idx="am-vio", ean="5900000005555")
    IntegrationEvent.objects.create(
        event_type=EventType.PHYSICAL_TOLERANCE_VIOLATION.value,
        severity=EventSeverity.WARNING.value,
        source_product=sp,
        message="seeded",
    )

    response = admin_client.get(reverse("admin-auto-matched-list"), {"has_violations": "true"})
    assert response.status_code == 200
    skus = {row["sku"] for row in response.json()["results"]}
    assert "AM-VIO-01" in skus
    row = next(r for r in response.json()["results"] if r["sku"] == "AM-VIO-01")
    assert row["has_tolerance_violation"] is True


def test_auto_matched_requires_authentication(api_client):
    response = api_client.get(reverse("admin-auto-matched-list"))
    assert response.status_code == 401
