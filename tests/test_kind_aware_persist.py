# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for kind-aware persist.

`_build_or_update_sp` routes RawProduct's price/attributes onto the field matching
`Source.kind` (cost/observed_price/signals) and always clears the other two so a kind
change never leaves a stale value behind. Matched monitoring/enrichment rows additionally
get an Observation row per run; unmatched rows stay in the review-list with none.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.enums import ProductStatus, SourceKind
from django_atlas.models import Observation, SourceProduct, SourceProductChangeLog
from django_atlas.schemas.contract import PriceStockUpdate, RawProduct
from django_atlas.services import import_service, log_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


# ============== FULL SYNC ==============


def test_full_sync_monitoring_writes_observed_price_clears_cost_and_signals():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source)
    raw = RawProduct(external_id="MON-1", name="Monitored", cost=Decimal("19.99"), currency="EUR")
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)
    sp = SourceProduct.objects.get(source=source, external_id="MON-1")
    assert sp.observed_price == Decimal("19.99")
    assert sp.cost is None
    assert sp.signals is None


def test_full_sync_enrichment_writes_signals_clears_cost_and_observed_price():
    source = SourceFactory(kind=SourceKind.ENRICHMENT.value)
    feed = FeedFactory(source=source)
    raw = RawProduct(external_id="ENR-1", name="Enriched", attributes={"tag": "demo", "popularity": "high"})
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)
    sp = SourceProduct.objects.get(source=source, external_id="ENR-1")
    assert sp.signals == {"tag": "demo", "popularity": "high"}
    assert sp.cost is None
    assert sp.observed_price is None


def test_full_sync_procurement_writes_cost_clears_observed_price_and_signals():
    source = SourceFactory(kind=SourceKind.PROCUREMENT.value)
    feed = FeedFactory(source=source)
    raw = RawProduct(external_id="PRO-1", name="Sourced", cost=Decimal("5.00"))
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)
    sp = SourceProduct.objects.get(source=source, external_id="PRO-1")
    assert sp.cost == Decimal("5.00")
    assert sp.observed_price is None
    assert sp.signals is None


def test_full_sync_kind_change_clears_stale_cross_kind_field():
    """A Source's kind change must not leave a stale cost/observed_price behind on
    re-sync (defense-in-depth alongside SourceProduct.clean(), which bulk paths bypass)."""
    source = SourceFactory(kind=SourceKind.PROCUREMENT.value)
    feed = FeedFactory(source=source)
    SourceProductFactory(source=source, feed=feed, external_id="X-1", cost=Decimal("10.00"), data={"a": 1})
    source.kind = SourceKind.MONITORING.value
    source.save(update_fields=["kind"])

    raw = RawProduct(external_id="X-1", name="X", cost=Decimal("11.00"), attributes={"a": 2})
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)

    sp = SourceProduct.objects.get(source=source, external_id="X-1")
    assert sp.observed_price == Decimal("11.00")
    assert sp.cost is None


def test_full_sync_monitoring_matched_sp_creates_observation_with_canonical_value():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source)
    rp = RealProduct.objects.create(sku="rp-matched-1")
    SourceProductFactory(source=source, feed=feed, external_id="MON-M1", real_product=rp)

    raw = RawProduct(external_id="MON-M1", name="Matched", cost=Decimal("29.99"), currency="PLN", stock=7)
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)

    obs = Observation.objects.get(source=source, sku="rp-matched-1")
    assert obs.kind == SourceKind.MONITORING.value
    assert obs.value == {"price": "29.99", "currency": "PLN", "stock": 7}


def test_full_sync_monitoring_unmatched_sp_creates_no_observation():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source)
    raw = RawProduct(external_id="MON-U1", name="Unmatched", cost=Decimal("9.99"))
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)
    sp = SourceProduct.objects.get(source=source, external_id="MON-U1")
    assert sp.real_product_id is None
    assert not Observation.objects.filter(source=source).exists()


def test_full_sync_enrichment_matched_sp_creates_observation_with_signals():
    source = SourceFactory(kind=SourceKind.ENRICHMENT.value)
    feed = FeedFactory(source=source)
    rp = RealProduct.objects.create(sku="rp-matched-2")
    SourceProductFactory(source=source, feed=feed, external_id="ENR-M1", real_product=rp, cost=None)

    raw = RawProduct(external_id="ENR-M1", name="Matched", attributes={"popularity": "high"})
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)

    obs = Observation.objects.get(source=source, sku="rp-matched-2")
    assert obs.kind == SourceKind.ENRICHMENT.value
    assert obs.value == {"signals": {"popularity": "high"}}


def test_full_sync_monitoring_stock_none_is_a_valid_observation_value():
    """`stock` absent from the feed row must land as an explicit `None` key, not be
    omitted — the canonical shape is `{price, currency, stock|null}`."""
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source)
    rp = RealProduct.objects.create(sku="rp-nostock")
    SourceProductFactory(source=source, feed=feed, external_id="MON-NS", real_product=rp)

    raw = RawProduct(external_id="MON-NS", name="No Stock", cost=Decimal("5.00"), currency="EUR", stock=None)
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, _fetch_connector([raw]), log)

    obs = Observation.objects.get(source=source, sku="rp-nostock")
    assert "stock" in obs.value
    assert obs.value["stock"] is None


# ============== DELTA SYNC ==============


def test_delta_sync_monitoring_writes_observed_price_not_cost():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source, sync_mode="delta")
    SourceProductFactory(source=source, feed=feed, external_id="MON-D1", cost=None, currency="EUR")
    upd = PriceStockUpdate(external_id="MON-D1", cost=Decimal("15.50"), currency="EUR", stock=3)
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, _delta_connector([upd]), log)
    sp = SourceProduct.objects.get(source=source, external_id="MON-D1")
    assert sp.observed_price == Decimal("15.50")
    assert sp.cost is None


def test_delta_sync_monitoring_matched_sp_appends_observation():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source, sync_mode="delta")
    rp = RealProduct.objects.create(sku="rp-delta-1")
    SourceProductFactory(source=source, feed=feed, external_id="MON-D2", real_product=rp, currency="EUR")
    upd = PriceStockUpdate(external_id="MON-D2", cost=Decimal("12.00"), currency="EUR", stock=1)
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, _delta_connector([upd]), log)
    obs = Observation.objects.get(source=source, sku="rp-delta-1")
    assert obs.value == {"price": "12.00", "currency": "EUR", "stock": 1}


def test_delta_sync_monitoring_observation_is_append_only_per_run():
    """A second delta run against the same matched SP appends a new row — it never
    overwrites the first (Observation.save() blocks updates)."""
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    feed = FeedFactory(source=source, sync_mode="delta")
    rp = RealProduct.objects.create(sku="rp-delta-2")
    SourceProductFactory(source=source, feed=feed, external_id="MON-D3", real_product=rp, currency="EUR")

    log1 = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(
        feed, _delta_connector([PriceStockUpdate(external_id="MON-D3", cost=Decimal("10.00"), currency="EUR")]), log1
    )
    log2 = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(
        feed, _delta_connector([PriceStockUpdate(external_id="MON-D3", cost=Decimal("11.00"), currency="EUR")]), log2
    )
    assert Observation.objects.filter(source=source, sku="rp-delta-2").count() == 2


def test_delta_sync_enrichment_is_noop_for_price_stock_update():
    """PriceStockUpdate has no `signals` concept — an enrichment source's delta sync must
    not write cost or observed_price for either field, and must not count/audit a change
    that never actually touched a persisted field (regression: `prev_price` used to fall
    back to `sp.cost`, which enrichment never writes, so a non-null `upd.cost` looked like
    a change against `None` even though nothing was written)."""
    source = SourceFactory(kind=SourceKind.ENRICHMENT.value)
    feed = FeedFactory(source=source, sync_mode="delta")
    SourceProductFactory(
        source=source,
        feed=feed,
        external_id="ENR-D1",
        cost=None,
        currency="EUR",
        status=ProductStatus.PUSHED.value,
    )
    upd = PriceStockUpdate(external_id="ENR-D1", cost=Decimal("99.00"), currency="EUR")
    log = log_service.start_import_log(feed, mode="delta")
    counts = import_service.process_delta_sync(feed, _delta_connector([upd]), log)
    sp = SourceProduct.objects.get(source=source, external_id="ENR-D1")
    assert sp.cost is None
    assert sp.observed_price is None
    assert counts["updated_count"] == 0
    assert counts["unchanged_count"] == 1
    assert not SourceProductChangeLog.objects.filter(source_product=sp).exists()


# ============== helpers ==============


def _fetch_connector(raws: list[RawProduct]):
    from tests.test_connector_hooks import _HookConnector

    return _HookConnector(raws=raws)


def _delta_connector(updates: list[PriceStockUpdate]):
    from tests.test_connector_hooks import _HookConnector

    return _HookConnector(deltas=updates)
