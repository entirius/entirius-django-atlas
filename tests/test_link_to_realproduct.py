# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the operator link endpoint + service (CMS "find in PIM" box).

The point of the service is that the SP itself is matched: a bare `SourceProductLink` leaves
`SourceProduct.real_product` NULL, so the row stays in the dedup candidate pool and a later
auto-push spawns a duplicate RealProduct.

django-lookup is not installed in this suite — `_record_link_verdict` is faked exactly like the
enrichment adapter's own touchpoints, and one test covers the "module absent" path for real.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django_pim.models.real_product import RealProduct

from django_atlas.enums import EventType, ProductStatus, SourceKind
from django_atlas.models import IntegrationEvent, SourceProductLink
from django_atlas.services import lookup_provider, product_link_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

_EAN = "5906214804074"


@pytest.fixture
def real_product():
    return RealProduct.objects.create(sku="AC-existing-01", ean=_EAN, weight=Decimal("0.150"))


@pytest.fixture
def source_product():
    source = SourceFactory(idx="acme", sku_prefix="AC")
    return SourceProductFactory(source=source, external_id="link-test-1", ean=_EAN, real_product=None)


@pytest.fixture
def verdicts(monkeypatch) -> list[tuple]:
    """Capture what the (absent) django-lookup dedup log would have been told."""
    recorded: list[tuple] = []
    monkeypatch.setattr(
        product_link_service,
        "_record_link_verdict",
        lambda sp, sku, user: recorded.append((lookup_provider.ref_for(sp), sku, user)),
    )
    return recorded


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_link_attaches_sp_and_creates_link(admin_user, real_product, source_product, verdicts):
    events: list[dict] = []

    result = product_link_service.link_sp_to_realproduct(
        source_product.pk, real_product.sku, admin_user, event_sink=events
    )

    source_product.refresh_from_db()
    assert result["real_product_sku"] == real_product.sku
    assert source_product.real_product_id == real_product.id

    link = SourceProductLink.objects.get(pk=result["link_pk"])
    assert (link.real_product_sku, link.source_id) == (real_product.sku, source_product.source_id)
    assert link.external_id == source_product.external_id
    # is_primary stays the operator's separate decision (set-primary endpoint).
    assert link.is_primary is False

    assert IntegrationEvent.objects.filter(event_type=EventType.LINKED_VIA_LOOKUP_UI.value).count() == 1
    assert [event["event_type"] for event in events] == [EventType.LINKED_VIA_LOOKUP_UI.value]
    assert verdicts == [("acme:link-test-1", real_product.sku, admin_user)]


def test_link_drops_the_sp_from_the_lookup_candidate_pool(admin_user, real_product, source_product, verdicts):
    """Linking is how the fingerprint row dies: the provider stops serving the item."""
    ref = lookup_provider.ref_for(source_product)
    assert ref in [item.ref for item in lookup_provider.iter_items()]

    product_link_service.link_sp_to_realproduct(source_product.pk, real_product.sku, admin_user)

    assert lookup_provider.candidates().filter(pk=source_product.pk).exists() is False
    assert ref not in [item.ref for item in lookup_provider.iter_items()]
    # The freshness signal watches real_product_id, so the save above triggers the refresh.
    assert "real_product_id" in lookup_provider.signal_specs()[0]["watch"]


def test_link_reuses_an_existing_link_for_the_same_sku_and_source(admin_user, real_product, source_product, verdicts):
    existing = SourceProductLink.objects.create(
        real_product_sku=real_product.sku, source=source_product.source, external_id="other", notes="operator note"
    )

    result = product_link_service.link_sp_to_realproduct(source_product.pk, real_product.sku, admin_user)

    existing.refresh_from_db()
    assert result["link_pk"] == existing.pk
    assert existing.notes == "operator note"
    assert SourceProductLink.objects.count() == 1


def test_link_allowed_for_a_monitoring_source(admin_user, real_product, verdicts):
    """No PIM row is created, so linking to an EXISTING RealProduct is not a push."""
    source = SourceFactory(idx="watcher", kind=SourceKind.MONITORING.value)
    sp = SourceProductFactory(source=source, external_id="watch-1", real_product=None)

    product_link_service.link_sp_to_realproduct(sp.pk, real_product.sku, admin_user)

    sp.refresh_from_db()
    assert sp.real_product_id == real_product.id


def test_link_survives_django_lookup_being_absent(admin_user, real_product, source_product):
    """No `verdicts` fixture here: the real touchpoint runs and fails on the missing module."""
    product_link_service.link_sp_to_realproduct(source_product.pk, real_product.sku, admin_user)

    source_product.refresh_from_db()
    assert source_product.real_product_id == real_product.id


def test_link_surfaces_a_real_dedup_log_failure(admin_user, real_product, source_product, monkeypatch):
    """Only the module being absent (ImportError) or unregistered in this dev checkout
    (RuntimeError, matching qms_writer._qms_available's own precedent) is swallowed — a genuine
    dedup_log failure (e.g. a DB error) must not be."""
    monkeypatch.setattr(
        product_link_service,
        "_record_link_verdict",
        lambda sp, sku, user: (_ for _ in ()).throw(ConnectionError("dedup_log unreachable")),
    )

    with pytest.raises(ConnectionError, match="dedup_log unreachable"):
        product_link_service.link_sp_to_realproduct(source_product.pk, real_product.sku, admin_user)

    # the link itself already happened before the verdict write — not rolled back by this failure.
    source_product.refresh_from_db()
    assert source_product.real_product_id == real_product.id


def test_link_raises_when_sp_missing(admin_user, real_product):
    with pytest.raises(ValueError, match="not found"):
        product_link_service.link_sp_to_realproduct(99999, real_product.sku, admin_user)


def test_link_raises_when_sku_missing(admin_user, source_product):
    with pytest.raises(ValueError, match="not found"):
        product_link_service.link_sp_to_realproduct(source_product.pk, "NOPE-1", admin_user)


def test_link_raises_when_sp_already_linked(admin_user, real_product, source_product, verdicts):
    source_product.real_product = real_product
    source_product.save(update_fields=["real_product"])

    with pytest.raises(ValueError, match="already linked"):
        product_link_service.link_sp_to_realproduct(source_product.pk, real_product.sku, admin_user)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _url(pk: int) -> str:
    return reverse("admin-products-link-to-realproduct", args=[pk])


def test_link_endpoint_happy_path(admin_client, real_product, source_product, verdicts):
    response = admin_client.post(_url(source_product.pk), {"real_product_sku": real_product.sku}, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["real_product_sku"] == real_product.sku
    assert SourceProductLink.objects.filter(pk=body["link_pk"]).exists()
    assert any(event["event_type"] == EventType.LINKED_VIA_LOOKUP_UI.value for event in body["events"])
    source_product.refresh_from_db()
    assert source_product.real_product_id == real_product.id


def test_link_endpoint_404_for_unknown_sp(admin_client, real_product):
    response = admin_client.post(_url(99999), {"real_product_sku": real_product.sku}, format="json")
    assert response.status_code == 404


def test_link_endpoint_404_for_unknown_sku(admin_client, source_product):
    response = admin_client.post(_url(source_product.pk), {"real_product_sku": "NOPE-1"}, format="json")
    assert response.status_code == 404


def test_link_endpoint_400_when_already_linked(admin_client, real_product, source_product, verdicts):
    source_product.real_product = real_product
    source_product.save(update_fields=["real_product"])

    response = admin_client.post(_url(source_product.pk), {"real_product_sku": real_product.sku}, format="json")
    assert response.status_code == 400


def test_link_endpoint_400_for_a_blank_sku(admin_client, source_product):
    response = admin_client.post(_url(source_product.pk), {"real_product_sku": ""}, format="json")
    assert response.status_code == 400


def test_link_endpoint_requires_admin(api_client, real_product, source_product):
    response = api_client.post(_url(source_product.pk), {"real_product_sku": real_product.sku}, format="json")
    assert response.status_code in (401, 403)


def test_link_endpoint_rejects_a_rejected_sp_stays_out_of_the_pool(admin_client, real_product, verdicts):
    """A rejected SP is not in the pool, but the operator may still link it by hand."""
    source = SourceFactory(idx="acme2")
    sp = SourceProductFactory(source=source, external_id="rej-1", status=ProductStatus.REJECTED.value)

    response = admin_client.post(_url(sp.pk), {"real_product_sku": real_product.sku}, format="json")

    assert response.status_code == 200
    sp.refresh_from_db()
    assert sp.real_product_id == real_product.id
