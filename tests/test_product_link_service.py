# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest
from django.db import IntegrityError, transaction

from django_atlas.models import SourceProductLink
from django_atlas.services import product_link_service
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def _create_pim_real_product(sku="psl-sku"):
    from django_pim.models.real_product import RealProduct

    return RealProduct.objects.create(sku=sku)


def test_create_link_validates_sku_in_pim():
    _create_pim_real_product("psl-1")
    sup = SourceFactory()
    link = product_link_service.create_link("psl-1", sup.idx)
    assert link.pk is not None
    assert link.real_product_sku == "psl-1"


def test_create_link_unknown_sku_raises():
    sup = SourceFactory()
    with pytest.raises(ValueError, match="not found"):
        product_link_service.create_link("ghost-sku", sup.idx)


def test_create_link_duplicate_raises():
    _create_pim_real_product("psl-dup")
    sup = SourceFactory()
    product_link_service.create_link("psl-dup", sup.idx)
    with pytest.raises(ValueError, match="already exists"):
        product_link_service.create_link("psl-dup", sup.idx)


def test_list_links_filter_by_sku():
    _create_pim_real_product("a")
    _create_pim_real_product("b")
    sup = SourceFactory()
    product_link_service.create_link("a", sup.idx)
    product_link_service.create_link("b", sup.idx)
    qs = product_link_service.list_links(real_product_sku="a")
    assert qs.count() == 1


def test_list_links_filter_by_source():
    _create_pim_real_product("c")
    sup1 = SourceFactory()
    sup2 = SourceFactory()
    product_link_service.create_link("c", sup1.idx)
    product_link_service.create_link("c", sup2.idx)
    qs = product_link_service.list_links(source_id=sup1.id)
    assert qs.count() == 1


def test_list_links_filter_by_is_active():
    _create_pim_real_product("d")
    sup = SourceFactory()
    link = product_link_service.create_link("d", sup.idx)
    link.is_active = False
    link.save(update_fields=["is_active"])
    assert product_link_service.list_links(is_active=False).count() == 1
    assert product_link_service.list_links(is_active=True).count() == 0


def test_update_link_changes_priority_primary_notes():
    _create_pim_real_product("e")
    sup = SourceFactory()
    link = product_link_service.create_link("e", sup.idx)
    updated = product_link_service.update_link(link.pk, priority=10, is_primary=True, notes="primary")
    assert updated.priority == 10
    assert updated.is_primary is True
    assert updated.notes == "primary"


def test_update_link_immutable_real_product_sku_raises():
    _create_pim_real_product("f")
    sup = SourceFactory()
    link = product_link_service.create_link("f", sup.idx)
    with pytest.raises(ValueError, match="not editable"):
        product_link_service.update_link(link.pk, real_product_sku="g")


def test_delete_link_removes_row():
    _create_pim_real_product("g")
    sup = SourceFactory()
    link = product_link_service.create_link("g", sup.idx)
    product_link_service.delete_link(link.pk)
    assert not SourceProductLink.objects.filter(pk=link.pk).exists()


def test_upsert_for_push_creates_without_pim_validation():
    """upsert_for_push assumes RealProduct exists (push just made it). No PIM validation."""
    sup = SourceFactory()
    link = product_link_service.upsert_for_push("not-yet-in-pim", sup, external_id="ext-1")
    assert link.pk is not None
    assert link.external_id == "ext-1"


def test_upsert_for_push_updates_existing_external_id_only():
    """upsert_for_push must NOT reset operator-controlled fields."""
    sup = SourceFactory()
    link = product_link_service.upsert_for_push("upsert-sku", sup, external_id="initial")
    # Operator changes priority + is_primary + notes.
    product_link_service.update_link(link.pk, priority=99, is_primary=True, notes="operator note")
    # Re-import (push) — should keep operator state.
    product_link_service.upsert_for_push("upsert-sku", sup, external_id="updated")
    link.refresh_from_db()
    assert link.external_id == "updated"
    assert link.priority == 99
    assert link.is_primary is True
    assert link.notes == "operator note"
    assert link.is_active is True


def test_set_primary_unsets_others_for_same_sku():
    _create_pim_real_product("pref-sku")
    sup1 = SourceFactory()
    sup2 = SourceFactory()
    link1 = product_link_service.create_link("pref-sku", sup1.idx)
    link2 = product_link_service.create_link("pref-sku", sup2.idx)

    product_link_service.set_primary(link1.pk)
    link1.refresh_from_db()
    link2.refresh_from_db()
    assert link1.is_primary is True
    assert link2.is_primary is False

    product_link_service.set_primary(link2.pk)
    link1.refresh_from_db()
    link2.refresh_from_db()
    assert link1.is_primary is False
    assert link2.is_primary is True


def test_create_link_primary_unsets_existing_primary_for_same_sku():
    _create_pim_real_product("excl-sku")
    sup1 = SourceFactory()
    sup2 = SourceFactory()
    link1 = product_link_service.create_link("excl-sku", sup1.idx, is_primary=True)
    link2 = product_link_service.create_link("excl-sku", sup2.idx, is_primary=True)
    link1.refresh_from_db()
    assert link1.is_primary is False
    assert link2.is_primary is True
    assert SourceProductLink.objects.filter(real_product_sku="excl-sku", is_primary=True).count() == 1


def test_update_link_primary_unsets_existing_primary_for_same_sku():
    _create_pim_real_product("excl-upd-sku")
    sup1 = SourceFactory()
    sup2 = SourceFactory()
    link1 = product_link_service.create_link("excl-upd-sku", sup1.idx, is_primary=True)
    link2 = product_link_service.create_link("excl-upd-sku", sup2.idx)

    product_link_service.update_link(link2.pk, is_primary=True)
    link1.refresh_from_db()
    link2.refresh_from_db()
    assert link1.is_primary is False
    assert link2.is_primary is True
    assert SourceProductLink.objects.filter(real_product_sku="excl-upd-sku", is_primary=True).count() == 1


def test_create_link_case_insensitive_sku_lookup():
    """RealProduct uses Lower(sku) unique constraint — lookup must match irrespective of case."""
    from django_pim.models.real_product import RealProduct

    RealProduct.objects.create(sku="abc-mixed")
    sup = SourceFactory()
    # Using uppercase variant — PIM stores lowercase normalized; iexact lookup matches.
    link = product_link_service.create_link("ABC-mixed", sup.idx)
    assert link.pk is not None


def test_source_cascade_deletes_links():
    _create_pim_real_product("cas-1")
    _create_pim_real_product("cas-2")
    sup = SourceFactory()
    product_link_service.create_link("cas-1", sup.idx)
    product_link_service.create_link("cas-2", sup.idx)
    sup.delete()
    assert SourceProductLink.objects.count() == 0


def test_update_link_rejects_unknown_field():
    _create_pim_real_product("ms-1")
    sup = SourceFactory()
    link = product_link_service.create_link("ms-1", sup.idx)
    with pytest.raises(ValueError, match="not editable"):
        product_link_service.update_link(link.pk, real_product_sku="other")
    with pytest.raises(ValueError, match="not editable"):
        product_link_service.update_link(link.pk, source=sup)


def test_two_primary_links_same_sku_violates_db_constraint_when_service_bypassed():
    """uq_sourceproductlink_one_primary_per_sku is the last line of defense.

    product_link_service.create_link/set_primary always unset the prior primary first,
    so this constraint is never hit through the service. Prove it still holds for a
    direct ORM write that skips the service (bulk_create, signal handler, management
    command bug, etc.).
    """
    _create_pim_real_product("two-primary-sku")
    sup_a = SourceFactory()
    sup_b = SourceFactory()
    SourceProductLink.objects.create(real_product_sku="two-primary-sku", source=sup_a, is_primary=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        SourceProductLink.objects.create(real_product_sku="two-primary-sku", source=sup_b, is_primary=True)
