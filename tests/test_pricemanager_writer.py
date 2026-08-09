# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for pricemanager_writer (D5 — no django_pricemanager import).

bulk events via record_bulk for performance scale.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from django_atlas.enums import EventType, ProductStatus, SourceKind
from django_atlas.models import IntegrationEvent, SourceSettings
from django_atlas.services import pricemanager_writer
from django_atlas.signals import cost_updated_signal
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _build_pushed_sp_with_rp(source, sku="pm-sku", cost=Decimal("99.99"), currency="EUR"):
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku=sku)
    sp = SourceProductFactory(source=source, status=ProductStatus.PUSHED.value, cost=cost, currency=currency)
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    return sp


def test_log_cost_emits_signal_per_channel():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup)

    received = []

    def _spy(sender, source_product, channel_idx, cost, currency, **kwargs):
        received.append((channel_idx, cost, currency))

    cost_updated_signal.connect(_spy)
    try:
        pricemanager_writer.log_cost(sp, sup, ["ch-a", "ch-b"], context="push")
    finally:
        cost_updated_signal.disconnect(_spy)
    assert {ch for ch, *_ in received} == {"ch-a", "ch-b"}


def test_log_cost_creates_cost_updated_event_per_channel():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup)
    pricemanager_writer.log_cost(sp, sup, ["ch-a", "ch-b", "ch-c"], context="push")
    events = IntegrationEvent.objects.filter(event_type=EventType.COST_UPDATED.value, source_product=sp)
    assert events.count() == 3


def test_cost_none_skips_with_warning():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup)
    sp.cost = None
    sp.save(update_fields=["cost"])
    pricemanager_writer.log_cost(sp, sup, ["ch-a"], context="push")
    assert IntegrationEvent.objects.filter(event_type=EventType.COST_MISSING.value, source_product=sp).exists()
    assert not IntegrationEvent.objects.filter(event_type=EventType.COST_UPDATED.value, source_product=sp).exists()


def test_currency_none_skips_with_warning():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup, currency="")
    pricemanager_writer.log_cost(sp, sup, ["ch-a"], context="push")
    assert IntegrationEvent.objects.filter(event_type=EventType.CURRENCY_MISSING.value, source_product=sp).exists()
    assert not IntegrationEvent.objects.filter(event_type=EventType.COST_UPDATED.value, source_product=sp).exists()


def test_delta_killswitch_skips():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup)
    settings = SourceSettings.load()
    settings.delta_sync_enabled = False
    settings.save()
    pricemanager_writer.log_cost(sp, sup, ["ch-a"], context="delta")
    assert not IntegrationEvent.objects.filter(source_product=sp).exists()


def test_multi_channel_two_signals_two_events():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup)
    received = []

    def _spy(sender, source_product, channel_idx, cost, currency, **kwargs):
        received.append(channel_idx)

    cost_updated_signal.connect(_spy)
    try:
        pricemanager_writer.log_cost(sp, sup, ["ch-1", "ch-2"], context="push")
    finally:
        cost_updated_signal.disconnect(_spy)
    assert len(received) == 2
    assert IntegrationEvent.objects.filter(event_type=EventType.COST_UPDATED.value, source_product=sp).count() == 2


def test_signal_payload_includes_cost_decimal_and_currency_str():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup, cost=Decimal("123.45"), currency="USD")
    captured = {}

    def _spy(sender, source_product, channel_idx, cost, currency, **kwargs):
        captured.update({"sp_id": source_product.id, "ch": channel_idx, "cost": cost, "currency": currency})

    cost_updated_signal.connect(_spy)
    try:
        pricemanager_writer.log_cost(sp, sup, ["ch-1"], context="push")
    finally:
        cost_updated_signal.disconnect(_spy)
    assert captured["cost"] == Decimal("123.45")
    assert captured["currency"] == "USD"


def test_event_details_include_cost_currency_sku_source_idx():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup, cost=Decimal("50.00"), currency="EUR", sku="detail-sku")
    pricemanager_writer.log_cost(sp, sup, ["ch-x"], context="delta")
    event = IntegrationEvent.objects.get(event_type=EventType.COST_UPDATED.value, source_product=sp)
    assert event.details["sku"] == "detail-sku"
    assert event.details["cost"] == "50.00"
    assert event.details["currency"] == "EUR"
    assert event.details["source_idx"] == sup.idx
    assert event.details["context"] == "delta"
    assert event.details["channel_idx"] == "ch-x"


def test_decimal_precision_preserved_in_details():
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup, cost=Decimal("299.9999"), currency="EUR", sku="prec-sku")
    pricemanager_writer.log_cost(sp, sup, ["ch-1"], context="push")
    event = IntegrationEvent.objects.get(event_type=EventType.COST_UPDATED.value, source_product=sp)
    assert event.details["cost"] == "299.9999"


def test_log_cost_non_procurement_source_is_noop():
    """Kind gate: monitoring/enrichment sources never emit cost_updated_signal or events."""
    sup = SourceFactory(kind=SourceKind.MONITORING.value)
    sp = _build_pushed_sp_with_rp(sup)
    received = []

    def _spy(sender, source_product, channel_idx, cost, currency, **kwargs):
        received.append(channel_idx)

    cost_updated_signal.connect(_spy)
    try:
        pricemanager_writer.log_cost(sp, sup, ["ch-a"], context="push")
    finally:
        cost_updated_signal.disconnect(_spy)
    assert received == []
    assert not IntegrationEvent.objects.filter(source_product=sp).exists()


def test_log_cost_missing_real_product_fk_skips_with_warning():
    """Second, independent gate: cost logging with no real_product FK -> COST_MISSING, no signal."""
    sup = SourceFactory()
    sp = SourceProductFactory(source=sup, status=ProductStatus.APPROVED.value, cost=Decimal("10.00"), currency="EUR")
    assert sp.real_product_id is None
    received = []

    def _spy(sender, source_product, channel_idx, cost, currency, **kwargs):
        received.append(channel_idx)

    cost_updated_signal.connect(_spy)
    try:
        pricemanager_writer.log_cost(sp, sup, ["ch-a"], context="push")
    finally:
        cost_updated_signal.disconnect(_spy)
    assert received == []
    assert IntegrationEvent.objects.filter(event_type=EventType.COST_MISSING.value, source_product=sp).exists()


def test_no_pricemanager_import_in_source():
    """D5 enforcement: source must not import django_pricemanager (mention in docstrings is OK)."""
    import pathlib

    src_path = pathlib.Path(pricemanager_writer.__file__)
    for line in src_path.read_text().splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import django_pricemanager"), f"forbidden import: {line}"
        assert not stripped.startswith("from django_pricemanager"), f"forbidden import: {line}"


def test_record_bulk_used_for_performance(django_assert_num_queries):
    """5-channel batch → single bulk INSERT (1 query) instead of 5 separate INSERTs."""
    sup = SourceFactory()
    sp = _build_pushed_sp_with_rp(sup, sku="bulk-sku")
    # Warm up — first call may evaluate lazy attrs / hit caches.
    pricemanager_writer.log_cost(sp, sup, ["ch-warmup"], context="push")
    # Now measure: 5 channels → expect 1 INSERT (record_bulk) for the IntegrationEvent rows.
    with patch.object(
        pricemanager_writer.event_service, "record_bulk", wraps=pricemanager_writer.event_service.record_bulk
    ) as wrapped:
        pricemanager_writer.log_cost(sp, sup, ["ch-1", "ch-2", "ch-3", "ch-4", "ch-5"], context="push")
        wrapped.assert_called_once()
        events_arg = wrapped.call_args.args[0] if wrapped.call_args.args else wrapped.call_args.kwargs["events"]
        assert len(events_arg) == 5
