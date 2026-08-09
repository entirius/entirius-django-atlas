# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for GET /admin/duplicates/."""

from decimal import Decimal

import pytest
from django.urls import reverse
from django_pim.models.real_product import RealProduct

from django_atlas.models import SourceProductLink
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def test_duplicates_empty_when_no_groups(admin_client):
    response = admin_client.get(reverse("admin-duplicates-list"))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 0
    assert body["results"] == []


def test_duplicates_returns_group_with_merge_suggestion(admin_client):
    ean = "5900000444444"
    sup = SourceFactory(idx="dup-api-sup")
    RealProduct.objects.create(sku="DAPI-A", ean=ean, weight=Decimal("0.150"))
    RealProduct.objects.create(sku="DAPI-B", ean=ean, weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku="DAPI-A", source=sup, is_primary=True)

    response = admin_client.get(reverse("admin-duplicates-list"))
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    group = body["results"][0]
    assert group["ean"] == ean
    assert group["suggestion"] == "merge"
    skus = {rp["sku"] for rp in group["realproducts"]}
    assert skus == {"DAPI-A", "DAPI-B"}
    snap_a = next(rp for rp in group["realproducts"] if rp["sku"] == "DAPI-A")
    assert any(s["idx"] == "dup-api-sup" and s["is_primary"] for s in snap_a["sources"])


def test_duplicates_rejects_invalid_tolerance(admin_client):
    response = admin_client.get(reverse("admin-duplicates-list"), {"tolerance_pct": "0.1"})
    assert response.status_code == 400, response.content


def test_duplicates_requires_authentication(api_client):
    response = api_client.get(reverse("admin-duplicates-list"))
    assert response.status_code == 401
