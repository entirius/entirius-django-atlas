# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for services.realproduct_merge_service."""

from decimal import Decimal

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.enums import ChangeLogSource, EventType
from django_atlas.models import IntegrationEvent, SourceProduct, SourceProductChangeLog, SourceProductLink
from django_atlas.services import realproduct_merge_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

_EAN = "5900000000777"


def _make_pair_with_links(weight_loser: Decimal = Decimal("0.155")):
    sup_a = SourceFactory(idx="merge-sup-a")
    sup_b = SourceFactory(idx="merge-sup-b")
    winner = RealProduct.objects.create(sku="WIN-001", ean=_EAN, weight=Decimal("0.150"))
    loser = RealProduct.objects.create(sku="LOSE-001", ean=_EAN, weight=weight_loser)
    SourceProductLink.objects.create(real_product_sku=winner.sku, source=sup_a, is_primary=True)
    SourceProductLink.objects.create(real_product_sku=loser.sku, source=sup_b, is_primary=False)
    sp_a = SourceProductFactory(source=sup_a, real_product=winner)
    sp_b = SourceProductFactory(source=sup_b, real_product=loser)
    return winner, loser, sup_a, sup_b, sp_a, sp_b


def test_merge_happy_path_redirects_links_and_deletes_loser():
    winner, loser, _, _, _, _ = _make_pair_with_links()

    result = realproduct_merge_service.merge_realproducts(
        winner_sku=winner.sku, loser_sku=loser.sku, reason="Same physical product"
    )

    assert result.winner_sku == winner.sku
    assert result.loser_sku == loser.sku
    assert result.links_redirected == 1
    assert result.source_products_repointed == 1
    assert not RealProduct.objects.filter(sku=loser.sku).exists()
    assert SourceProductLink.objects.filter(real_product_sku=winner.sku).count() == 2
    assert not SourceProductLink.objects.filter(real_product_sku=loser.sku).exists()
    assert SourceProduct.objects.filter(real_product__sku=winner.sku).count() == 2


def test_merge_writes_audit_and_event():
    winner, loser, _, _, _, _ = _make_pair_with_links()

    result = realproduct_merge_service.merge_realproducts(
        winner_sku=winner.sku, loser_sku=loser.sku, reason="Operator reconciliation"
    )

    assert result.audit_id is not None
    audit = SourceProductChangeLog.objects.get(id=result.audit_id)
    assert audit.source == ChangeLogSource.MANUAL_MERGE.value
    assert audit.field_path == "realproduct.merge"
    assert audit.before == {"sku": loser.sku, "ean": _EAN}
    assert audit.after["sku"] == winner.sku
    assert audit.after["reason"] == "Operator reconciliation"
    assert audit.real_product_sku == winner.sku

    event = IntegrationEvent.objects.get(event_type=EventType.REALPRODUCT_MANUALLY_MERGED.value)
    assert event.severity == "info"
    assert event.details["winner_sku"] == winner.sku
    assert event.details["loser_sku"] == loser.sku
    assert event.details["links_redirected"] == 1


def test_merge_rejects_ean_mismatch():
    sup = SourceFactory(idx="mm-sup")
    winner = RealProduct.objects.create(sku="MM-WIN", ean="5900000000001", weight=Decimal("0.150"))
    loser = RealProduct.objects.create(sku="MM-LOSE", ean="5900000000002", weight=Decimal("0.155"))
    SourceProductLink.objects.create(real_product_sku=loser.sku, source=sup, is_primary=False)

    with pytest.raises(ValueError, match="EAN mismatch"):
        realproduct_merge_service.merge_realproducts(winner_sku=winner.sku, loser_sku=loser.sku, reason="Should fail")

    # Nothing committed: loser still around
    assert RealProduct.objects.filter(sku=loser.sku).exists()
    assert SourceProductLink.objects.filter(real_product_sku=loser.sku).exists()


def test_merge_rejects_winner_equals_loser():
    sup = SourceFactory(idx="eq-sup")
    rp = RealProduct.objects.create(sku="EQ-001", ean=_EAN, weight=Decimal("0.150"))
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=sup, is_primary=True)

    with pytest.raises(ValueError, match="must differ"):
        realproduct_merge_service.merge_realproducts(winner_sku=rp.sku, loser_sku=rp.sku, reason="No-op attempt")


def test_merge_rejects_short_reason():
    with pytest.raises(ValueError, match="at least 3"):
        realproduct_merge_service.merge_realproducts(winner_sku="X", loser_sku="Y", reason="ok")


def test_merge_raises_when_winner_missing():
    sup = SourceFactory(idx="nf-sup")
    loser = RealProduct.objects.create(sku="NF-LOSE", ean=_EAN, weight=Decimal("0.150"))
    SourceProductLink.objects.create(real_product_sku=loser.sku, source=sup, is_primary=False)

    with pytest.raises(RealProduct.DoesNotExist):
        realproduct_merge_service.merge_realproducts(
            winner_sku="DOES-NOT-EXIST", loser_sku=loser.sku, reason="Should not happen"
        )
