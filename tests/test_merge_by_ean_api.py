# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for POST /admin/realproducts/merge-by-ean/."""

from decimal import Decimal

import pytest
from django.urls import reverse
from django_pim.models.real_product import RealProduct

from django_atlas.models import SourceProductChangeLog, SourceProductLink
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

_EAN = "5900000099999"


def _setup_merge_pair():
    sup_a = SourceFactory(idx="api-mrg-a")
    sup_b = SourceFactory(idx="api-mrg-b")
    winner = RealProduct.objects.create(sku="API-WIN", ean=_EAN, weight=Decimal("0.150"))
    loser = RealProduct.objects.create(sku="API-LOSE", ean=_EAN, weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku=winner.sku, source=sup_a, is_primary=True)
    SourceProductLink.objects.create(real_product_sku=loser.sku, source=sup_b, is_primary=False)
    SourceProductFactory(source=sup_a, real_product=winner)
    SourceProductFactory(source=sup_b, real_product=loser)
    return winner, loser


def test_merge_by_ean_happy_path_returns_200(admin_client):
    winner, loser = _setup_merge_pair()
    url = reverse("admin-realproducts-merge-by-ean")
    response = admin_client.post(
        url, {"winner_sku": winner.sku, "loser_sku": loser.sku, "reason": "Same physical product"}, format="json"
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["winner_sku"] == winner.sku
    assert data["loser_sku"] == loser.sku
    assert data["links_redirected"] == 1
    assert data["source_products_repointed"] == 1
    assert data["audit_id"] is not None
    assert not RealProduct.objects.filter(sku=loser.sku).exists()
    assert SourceProductChangeLog.objects.filter(source="manual_merge").count() == 1


def test_merge_by_ean_rejects_short_reason_400(admin_client):
    winner, loser = _setup_merge_pair()
    url = reverse("admin-realproducts-merge-by-ean")
    response = admin_client.post(url, {"winner_sku": winner.sku, "loser_sku": loser.sku, "reason": "x"}, format="json")
    assert response.status_code == 400, response.content
    body = response.json()
    assert "debug_id" in body


def test_merge_by_ean_rejects_ean_mismatch_400(admin_client):
    sup_a = SourceFactory(idx="mm-a")
    sup_b = SourceFactory(idx="mm-b")
    winner = RealProduct.objects.create(sku="MM-WIN", ean="5900000000001", weight=Decimal("0.150"))
    loser = RealProduct.objects.create(sku="MM-LOSE", ean="5900000000002", weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku=winner.sku, source=sup_a, is_primary=True)
    SourceProductLink.objects.create(real_product_sku=loser.sku, source=sup_b, is_primary=False)

    url = reverse("admin-realproducts-merge-by-ean")
    response = admin_client.post(
        url, {"winner_sku": winner.sku, "loser_sku": loser.sku, "reason": "Should fail"}, format="json"
    )
    assert response.status_code == 400, response.content


def test_merge_by_ean_returns_404_for_unknown_sku(admin_client):
    _setup_merge_pair()
    url = reverse("admin-realproducts-merge-by-ean")
    response = admin_client.post(
        url, {"winner_sku": "DOES-NOT-EXIST", "loser_sku": "API-LOSE", "reason": "Missing winner"}, format="json"
    )
    assert response.status_code == 404, response.content


def test_merge_by_ean_requires_authentication(api_client):
    url = reverse("admin-realproducts-merge-by-ean")
    response = api_client.post(url, {"winner_sku": "A", "loser_sku": "B", "reason": "noauth"}, format="json")
    assert response.status_code == 401
