# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Coverage for observation_service.

Append-only contract, canonical `value` shape round-trip, market context
resolved via `source.country`/`source.currency` (never duplicated on
Observation), get_observations sort/filter + latest_per_source, and the
batch API (get_skus_with_valid_observations / get_observations_bulk —
N+1-safety proven with django_assert_max_num_queries, not just output shape).
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from django_atlas.enums import SourceKind
from django_atlas.models import Observation
from django_atlas.services import observation_service
from django_atlas.signals.definitions import observation_recorded_signal
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db


def _monitoring_source(**kwargs):
    return SourceFactory(kind=SourceKind.MONITORING.value, **kwargs)


def _enrichment_source(**kwargs):
    return SourceFactory(kind=SourceKind.ENRICHMENT.value, **kwargs)


# --- record_observation: append-only write choke point -----------------------------------


def test_record_observation_denormalizes_kind_from_source():
    source = _monitoring_source()
    value = {"price": "19.99", "currency": "EUR", "stock": 3}
    observation = observation_service.record_observation(source=source, sku="SKU-1", value=value)
    assert observation.kind == SourceKind.MONITORING.value
    assert observation.source_id == source.id


def test_record_observation_requires_sku():
    source = _monitoring_source()
    with pytest.raises(ValueError, match="sku is required"):
        observation_service.record_observation(source=source, sku="", value={"price": "1", "currency": "EUR"})


def test_record_observation_two_calls_create_two_rows_not_upsert():
    source = _monitoring_source()
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "10.00", "currency": "EUR"})
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "11.00", "currency": "EUR"})
    assert Observation.objects.filter(source=source, sku="SKU-1").count() == 2


def test_record_observation_accepts_custom_ts_for_backfill():
    source = _monitoring_source()
    custom_ts = timezone.now() - timedelta(days=30)
    observation = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=custom_ts
    )
    assert observation.ts == custom_ts


def test_observation_is_append_only_update_raises():
    source = _monitoring_source()
    observation = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"}
    )
    loaded = Observation.objects.get(pk=observation.pk)
    loaded.value = {"price": "999", "currency": "EUR"}
    with pytest.raises(ValueError, match="append-only"):
        loaded.save()


def test_record_observation_stores_raw_payload():
    source = _enrichment_source()
    raw = {"vendor_payload": {"foo": "bar"}}
    observation = observation_service.record_observation(
        source=source, sku="SKU-1", value={"signals": {"popularity": 42}}, raw=raw
    )
    assert observation.raw == raw


def test_record_observation_emits_signal():
    source = _monitoring_source()
    receiver = MagicMock()
    observation_recorded_signal.connect(receiver, dispatch_uid="test-record-observation-signal")
    try:
        observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    finally:
        observation_recorded_signal.disconnect(dispatch_uid="test-record-observation-signal")
    assert receiver.called
    _, kwargs = receiver.call_args
    assert kwargs["source"] == source
    assert kwargs["sku"] == "SKU-1"
    assert kwargs["kind"] == SourceKind.MONITORING.value
    assert kwargs["value"] == {"price": "1", "currency": "EUR"}


# --- canonical value shape validation -------------------------------------------------------


def test_record_observation_monitoring_rejects_missing_price():
    source = _monitoring_source()
    with pytest.raises(ValueError, match="require a 'price' key"):
        observation_service.record_observation(source=source, sku="SKU-1", value={"currency": "EUR"})


def test_record_observation_monitoring_rejects_non_numeric_price():
    source = _monitoring_source()
    with pytest.raises(ValueError, match="require a 'price' key"):
        observation_service.record_observation(source=source, sku="SKU-1", value={"price": None})


def test_record_observation_enrichment_rejects_missing_signals():
    source = _enrichment_source()
    with pytest.raises(ValueError, match="require a 'signals' dict"):
        observation_service.record_observation(source=source, sku="SKU-1", value={"foo": "bar"})


def test_record_observation_enrichment_rejects_non_dict_signals():
    source = _enrichment_source()
    with pytest.raises(ValueError, match="require a 'signals' dict"):
        observation_service.record_observation(source=source, sku="SKU-1", value={"signals": "not-a-dict"})


def test_record_observation_rejects_non_dict_value():
    source = _monitoring_source()
    with pytest.raises(ValueError, match="value must be a dict"):
        observation_service.record_observation(source=source, sku="SKU-1", value="not-a-dict")


# --- canonical value shape round-trip -----------------------------------------------------


def test_monitoring_value_shape_roundtrips_through_jsonfield():
    source = _monitoring_source()
    value = {"price": "199.99", "currency": "USD", "stock": None}
    observation_service.record_observation(source=source, sku="SKU-1", value=value)
    stored = Observation.objects.get(source=source, sku="SKU-1")
    assert stored.value == value


def test_enrichment_value_shape_roundtrips_through_jsonfield():
    source = _enrichment_source()
    value = {"signals": {"trend_score": 0.87, "tags": ["seasonal", "promo"]}}
    observation_service.record_observation(source=source, sku="SKU-1", value=value)
    stored = Observation.objects.get(source=source, sku="SKU-1")
    assert stored.value == value


# --- market context inherited from Source, not duplicated -------------------------------


def test_market_context_resolved_via_source_not_stored_on_observation(country, currency):
    source = _monitoring_source(country=country, currency=currency)
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    results = observation_service.get_observations("SKU-1", SourceKind.MONITORING.value)
    assert len(results) == 1
    resolved_source = results[0]["source"]
    assert resolved_source.country_id == country.id
    assert resolved_source.currency_id == currency.id
    assert not hasattr(Observation, "country")
    assert not hasattr(Observation, "currency")


# --- get_observations: empty log, sort/filter, latest_per_source -------------------------


def test_get_observations_empty_log_returns_empty_list_never_raises():
    assert observation_service.get_observations("no-such-sku", SourceKind.MONITORING.value) == []


def test_get_observations_full_timeline_newest_first():
    source = _monitoring_source()
    old = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
    )
    new = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "2", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    results = observation_service.get_observations("SKU-1", SourceKind.MONITORING.value, latest_per_source=False)
    assert [r["ts"] for r in results] == [new.ts, old.ts]


def test_get_observations_latest_per_source_returns_one_row_per_source():
    source_a = _monitoring_source()
    source_b = _monitoring_source()
    observation_service.record_observation(
        source=source_a, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
    )
    latest_a = observation_service.record_observation(
        source=source_a, sku="SKU-1", value={"price": "1.50", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    latest_b = observation_service.record_observation(
        source=source_b, sku="SKU-1", value={"price": "9", "currency": "EUR"}
    )

    results = observation_service.get_observations("SKU-1", SourceKind.MONITORING.value)

    assert len(results) == 2
    by_source = {r["source"].id: r for r in results}
    assert by_source[source_a.id]["value"] == latest_a.value
    assert by_source[source_b.id]["value"] == latest_b.value


def test_get_observations_filters_by_kind():
    source = _monitoring_source()
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    assert observation_service.get_observations("SKU-1", SourceKind.ENRICHMENT.value) == []


# --- get_skus_with_valid_observations: batch entry point, N+1-safe -----------------------


def test_get_skus_with_valid_observations_filters_by_max_age():
    source = _monitoring_source()
    observation_service.record_observation(
        source=source, sku="FRESH", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    observation_service.record_observation(
        source=source, sku="STALE", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(days=10)
    )
    skus = observation_service.get_skus_with_valid_observations(SourceKind.MONITORING.value, timedelta(days=1))
    assert skus == {"FRESH"}


def test_get_skus_with_valid_observations_single_query(django_assert_max_num_queries):
    source = _monitoring_source()
    for i in range(20):
        observation_service.record_observation(source=source, sku=f"SKU-{i}", value={"price": "1", "currency": "EUR"})
    with django_assert_max_num_queries(1):
        skus = observation_service.get_skus_with_valid_observations(SourceKind.MONITORING.value, timedelta(days=1))
    assert len(skus) == 20


# --- get_observations_bulk: N+1-safe, latest-per-source across many skus -----------------


def test_get_observations_bulk_empty_skus_returns_empty_dict():
    assert observation_service.get_observations_bulk([], SourceKind.MONITORING.value) == {}


def test_get_observations_bulk_omits_skus_with_no_observations():
    source = _monitoring_source()
    observation_service.record_observation(source=source, sku="HAS-DATA", value={"price": "1", "currency": "EUR"})
    result = observation_service.get_observations_bulk(["HAS-DATA", "NO-DATA"], SourceKind.MONITORING.value)
    assert "HAS-DATA" in result
    assert "NO-DATA" not in result


def test_get_observations_bulk_matches_single_sku_contract_for_missing_data():
    """Consistency: single-sku 'nothing found' = [], batch 'nothing found' = key absent."""
    single = observation_service.get_observations("NO-DATA", SourceKind.MONITORING.value)
    bulk = observation_service.get_observations_bulk(["NO-DATA"], SourceKind.MONITORING.value)
    assert single == []
    assert bulk == {}


def test_get_observations_bulk_latest_per_source_one_query_for_many_skus(django_assert_max_num_queries):
    source_a = _monitoring_source()
    source_b = _monitoring_source()
    skus = [f"SKU-{i}" for i in range(15)]
    for sku in skus:
        observation_service.record_observation(
            source=source_a, sku=sku, value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
        )
        observation_service.record_observation(
            source=source_a, sku=sku, value={"price": "2", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
        )
        observation_service.record_observation(source=source_b, sku=sku, value={"price": "9", "currency": "EUR"})

    with django_assert_max_num_queries(1):
        result = observation_service.get_observations_bulk(skus, SourceKind.MONITORING.value)

    assert set(result.keys()) == set(skus)
    for sku in skus:
        assert len(result[sku]) == 2
        by_source = {entry["source"].id: entry["value"] for entry in result[sku]}
        assert by_source[source_a.id] == {"price": "2", "currency": "EUR"}
        assert by_source[source_b.id] == {"price": "9", "currency": "EUR"}


def test_get_observations_bulk_full_timeline_newest_first_per_sku():
    source = _monitoring_source()
    old = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
    )
    new = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "2", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    result = observation_service.get_observations_bulk(["SKU-1"], SourceKind.MONITORING.value, latest_per_source=False)
    assert [entry["ts"] for entry in result["SKU-1"]] == [new.ts, old.ts]


# --- list_observations_queryset: admin-browse queryset -------


def test_list_observations_queryset_empty_returns_empty():
    assert list(observation_service.list_observations_queryset()) == []


def test_list_observations_queryset_filters_by_sku():
    source = _monitoring_source()
    observation_service.record_observation(source=source, sku="SKU-A", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source, sku="SKU-B", value={"price": "2", "currency": "EUR"})
    qs = observation_service.list_observations_queryset(sku="SKU-A")
    assert [o.sku for o in qs] == ["SKU-A"]


def test_list_observations_queryset_filters_by_kind():
    mon = _monitoring_source()
    enr = _enrichment_source()
    observation_service.record_observation(source=mon, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=enr, sku="SKU-1", value={"signals": {"tag": "x"}})
    qs = observation_service.list_observations_queryset(sku="SKU-1", kind=SourceKind.ENRICHMENT.value)
    assert [o.kind for o in qs] == [SourceKind.ENRICHMENT.value]


def test_list_observations_queryset_filters_by_source_idx():
    source_a = _monitoring_source()
    source_b = _monitoring_source()
    observation_service.record_observation(source=source_a, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source_b, sku="SKU-1", value={"price": "2", "currency": "EUR"})
    qs = observation_service.list_observations_queryset(source_idx=source_a.idx)
    assert [o.source_id for o in qs] == [source_a.id]


def test_list_observations_queryset_default_full_timeline_newest_first():
    source = _monitoring_source()
    old = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
    )
    new = observation_service.record_observation(
        source=source, sku="SKU-1", value={"price": "2", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    qs = observation_service.list_observations_queryset(sku="SKU-1")
    assert list(qs) == [new, old]


def test_list_observations_queryset_latest_per_source_collapses_timeline():
    source_a = _monitoring_source()
    source_b = _monitoring_source()
    observation_service.record_observation(
        source=source_a, sku="SKU-1", value={"price": "1", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=2)
    )
    observation_service.record_observation(
        source=source_a, sku="SKU-1", value={"price": "2", "currency": "EUR"}, ts=timezone.now() - timedelta(hours=1)
    )
    observation_service.record_observation(source=source_b, sku="SKU-1", value={"price": "9", "currency": "EUR"})
    qs = observation_service.list_observations_queryset(sku="SKU-1", latest_per_source=True)
    assert qs.count() == 2
    values_by_source = {o.source_id: o.value for o in qs}
    assert values_by_source[source_a.id] == {"price": "2", "currency": "EUR"}
    assert values_by_source[source_b.id] == {"price": "9", "currency": "EUR"}
