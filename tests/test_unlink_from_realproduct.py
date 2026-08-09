# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the operator force-unlink endpoint + service."""

from decimal import Decimal

import pytest
from django.urls import reverse
from django_pim.models.real_product import RealProduct

from django_atlas.enums import ChangeLogSource, EventType
from django_atlas.models import IntegrationEvent, SourceProductChangeLog, SourceProductLink
from django_atlas.services import product_link_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

_EAN = "5906214804074"


def _seed_auto_linked_sp(sku_prefix: str = "AC") -> tuple:
    """Create RP + SP + SourceProductLink representing an auto-linked SP."""
    rp = RealProduct.objects.create(sku="AC-existing-01", ean=_EAN, weight=Decimal("0.150"))
    source = SourceFactory(sku_prefix=sku_prefix)
    sp = SourceProductFactory(source=source, external_id="unlink-test-1", ean=_EAN)
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    link = SourceProductLink.objects.create(
        real_product_sku=rp.sku, source=source, external_id=sp.external_id, is_primary=False
    )
    return rp, sp, source, link


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_unlink_creates_fresh_rp_and_moves_sp(admin_user):
    rp, sp, source, link = _seed_auto_linked_sp()
    old_link_pk = link.pk

    events: list[dict] = []
    result = product_link_service.unlink_sp_from_realproduct(sp.pk, admin_user, event_sink=events)

    sp.refresh_from_db()
    assert result["previous_real_product_sku"] == rp.sku
    assert result["new_real_product_sku"].startswith("AC-")
    assert sp.real_product.sku == result["new_real_product_sku"]

    # Old link gone, new link is primary=True.
    assert not SourceProductLink.objects.filter(pk=old_link_pk).exists()
    new_link = SourceProductLink.objects.get(source=source)
    assert new_link.real_product_sku == result["new_real_product_sku"]
    assert new_link.is_primary is True

    # Audit row written.
    audit = SourceProductChangeLog.objects.filter(source_product=sp, source=ChangeLogSource.MANUAL_UNLINK.value)
    assert audit.count() == 1
    row = audit.first()
    assert row.before == {"sku": rp.sku}
    assert row.after["sku"] == result["new_real_product_sku"]
    assert row.triggered_by_id == admin_user.id

    # IntegrationEvent + event_sink populated.
    assert IntegrationEvent.objects.filter(event_type=EventType.MANUAL_UNLINK_FROM_REALPRODUCT.value).count() == 1
    assert any(e["event_type"] == EventType.MANUAL_UNLINK_FROM_REALPRODUCT.value for e in events)


def test_unlink_raises_when_sp_missing(admin_user):
    with pytest.raises(ValueError, match="not found"):
        product_link_service.unlink_sp_from_realproduct(99999, admin_user)


def test_unlink_raises_when_sp_not_linked(admin_user):
    source = SourceFactory()
    sp = SourceProductFactory(source=source, real_product=None)
    with pytest.raises(ValueError, match="not linked"):
        product_link_service.unlink_sp_from_realproduct(sp.pk, admin_user)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_unlink_endpoint_happy_path(admin_client):
    rp, sp, source, _ = _seed_auto_linked_sp()
    url = reverse("admin-products-unlink-from-realproduct", args=[sp.pk])

    response = admin_client.post(url)

    assert response.status_code == 200
    body = response.json()
    assert body["previous_real_product_sku"] == rp.sku
    assert body["new_real_product_sku"].startswith("AC-")
    assert any(e["event_type"] == EventType.MANUAL_UNLINK_FROM_REALPRODUCT.value for e in body["events"])


def test_unlink_endpoint_404_for_unknown_sp(admin_client):
    url = reverse("admin-products-unlink-from-realproduct", args=[99999])
    response = admin_client.post(url)
    assert response.status_code == 404


def test_unlink_endpoint_400_when_not_linked(admin_client):
    source = SourceFactory()
    sp = SourceProductFactory(source=source, real_product=None)
    url = reverse("admin-products-unlink-from-realproduct", args=[sp.pk])
    response = admin_client.post(url)
    assert response.status_code == 400
