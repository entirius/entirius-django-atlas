# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for physical race detection.

Hits `_apply_physical_update_to_real_product` directly, bypassing the delta sync harness.
Covers the four outcome branches: applied (primary), skipped_non_primary, overwritten
(opt-in), noop (no link → still skipped per spec). Integration coverage of the counts
dispatcher lives in `test_delta_sync_race.py`.
"""

from decimal import Decimal

import pytest

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType, ProductStatus
from django_atlas.models import IntegrationEvent, SourceProductChangeLog, SourceProductLink
from django_atlas.schemas.contract import PriceStockUpdate
from django_atlas.services import import_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _make_sp_with_real_product(*, source, sku: str, initial_weight: Decimal):
    """Helper — creates RealProduct + SourceProduct + wires the FK."""
    from django_pim.models.real_product import RealProduct

    feed = FeedFactory(source=source, sync_mode="delta")
    rp = RealProduct.objects.create(sku=sku, weight=initial_weight)
    sp = SourceProductFactory(source=source, feed=feed, external_id=f"EXT-{sku}", status=ProductStatus.PUSHED.value)
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    return sp, rp


def test_primary_link_applies_physical_change():
    """Baseline — primary source writes through to RealProduct + emits applied event."""
    source = SourceFactory()
    sp, rp = _make_sp_with_real_product(source=source, sku="race-pref-001", initial_weight=Decimal("0.15"))
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=source, is_primary=True, is_active=True)

    upd = PriceStockUpdate(external_id=sp.external_id, physical={"weight": "0.25"})
    outcome = import_service._apply_physical_update_to_real_product(sp, upd, started_at=None)

    assert outcome == import_service.PHYSICAL_OUTCOME_APPLIED
    rp.refresh_from_db()
    assert rp.weight == Decimal("0.25")
    audit = SourceProductChangeLog.objects.get(field_path="physical.weight", source_product=sp)
    assert audit.source == ChangeLogSource.DELTA_SYNC.value
    assert audit.applied_to_pim is True
    event = IntegrationEvent.objects.get(event_type=EventType.PHYSICAL_UPDATE_APPLIED.value, source_product=sp)
    assert event.severity == EventSeverity.INFO.value


def test_non_primary_link_default_skips_write():
    """Non-primary source → RealProduct unchanged + info skip event + physical_skipped audit."""
    primary_source = SourceFactory(idx="race-primary")
    non_pref_source = SourceFactory(idx="race-non-primary")
    sp, rp = _make_sp_with_real_product(source=non_pref_source, sku="race-skip-001", initial_weight=Decimal("0.15"))
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=primary_source, is_primary=True, is_active=True)
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=non_pref_source, is_primary=False, is_active=True)

    upd = PriceStockUpdate(external_id=sp.external_id, physical={"weight": "2.50"})
    outcome = import_service._apply_physical_update_to_real_product(sp, upd, started_at=None)

    assert outcome == import_service.PHYSICAL_OUTCOME_SKIPPED_NON_PRIMARY
    rp.refresh_from_db()
    assert rp.weight == Decimal("0.15"), "RealProduct.weight MUST NOT change on race skip"
    event = IntegrationEvent.objects.get(
        event_type=EventType.PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY.value, source_product=sp
    )
    assert event.severity == EventSeverity.INFO.value
    assert event.details["primary_source_idx"] == "race-primary"
    assert event.details["source_idx"] == "race-non-primary"
    assert event.details["attempted_fields"] == ["weight"]
    audit = SourceProductChangeLog.objects.get(source=ChangeLogSource.PHYSICAL_SKIPPED.value, source_product=sp)
    assert audit.applied_to_pim is False
    assert audit.field_path == "physical_skipped"
    assert audit.after == {"weight": "2.50"}
    # No physical.weight audit row should exist for this SP.
    assert not SourceProductChangeLog.objects.filter(source_product=sp, field_path="physical.weight").exists()


def test_non_primary_link_with_opt_in_overwrites():
    """allow_physical_writes_from_non_primary=True → write lands + warning event + physical_overwrite audit."""
    primary_source = SourceFactory(idx="race-pref-opt")
    non_pref_source = SourceFactory(idx="race-opt-in", allow_physical_writes_from_non_primary=True)
    sp, rp = _make_sp_with_real_product(
        source=non_pref_source, sku="race-overwrite-001", initial_weight=Decimal("0.15")
    )
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=primary_source, is_primary=True, is_active=True)
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=non_pref_source, is_primary=False, is_active=True)

    upd = PriceStockUpdate(external_id=sp.external_id, physical={"weight": "3.00"})
    outcome = import_service._apply_physical_update_to_real_product(sp, upd, started_at=None)

    assert outcome == import_service.PHYSICAL_OUTCOME_OVERWRITTEN
    rp.refresh_from_db()
    assert rp.weight == Decimal("3.00")
    audit = SourceProductChangeLog.objects.get(source=ChangeLogSource.PHYSICAL_OVERWRITE.value, source_product=sp)
    assert audit.applied_to_pim is True
    assert audit.field_path == "physical.weight"
    event = IntegrationEvent.objects.get(event_type=EventType.PHYSICAL_UPDATE_OVERWRITE.value, source_product=sp)
    assert event.severity == EventSeverity.WARNING.value
    assert event.details["primary_source_idx"] == "race-pref-opt"
    assert event.details["non_primary_source_idx"] == "race-opt-in"


def test_no_link_existing_treated_as_non_primary_skip():
    """Edge case — SP has a RealProduct but no SourceProductLink row exists yet.

    In a healthy push flow the link is created by `upsert_for_push`, but legacy data /
    race conditions / cleanup scripts can produce orphan SPs. Default behaviour MUST
    be skip (primary is the source of truth — and there is no primary here).
    """
    source = SourceFactory(idx="race-orphan")
    sp, rp = _make_sp_with_real_product(source=source, sku="race-orphan-001", initial_weight=Decimal("0.15"))

    upd = PriceStockUpdate(external_id=sp.external_id, physical={"weight": "2.50"})
    outcome = import_service._apply_physical_update_to_real_product(sp, upd, started_at=None)

    assert outcome == import_service.PHYSICAL_OUTCOME_SKIPPED_NON_PRIMARY
    rp.refresh_from_db()
    assert rp.weight == Decimal("0.15")
    assert IntegrationEvent.objects.filter(
        event_type=EventType.PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY.value, source_product=sp
    ).exists()


def test_skip_path_details_payload_shape():
    """Lock the IntegrationEvent.details schema on the skip path — CMS will key off this."""
    primary_source = SourceFactory(idx="race-details-pref")
    non_pref_source = SourceFactory(idx="race-details-non")
    sp, rp = _make_sp_with_real_product(source=non_pref_source, sku="race-details-001", initial_weight=Decimal("0.15"))
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=primary_source, is_primary=True, is_active=True)
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=non_pref_source, is_primary=False, is_active=True)

    upd = PriceStockUpdate(
        external_id=sp.external_id, physical={"weight": "2.50", "ean": "5901234567890", "width": None}
    )
    import_service._apply_physical_update_to_real_product(sp, upd, started_at=None)

    event = IntegrationEvent.objects.get(
        event_type=EventType.PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY.value, source_product=sp
    )
    details = event.details
    assert set(details.keys()) >= {"sku", "source_idx", "primary_source_idx", "attempted_fields"}
    assert details["attempted_fields"] == ["ean", "weight"], "Only non-None fields, sorted"
    assert details["sku"] == "race-details-001"
