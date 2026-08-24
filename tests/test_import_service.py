# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for import_service (full sync, delta sync, async dispatch, errors, signals)."""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from django_atlas.connectors.base import SyncConnector
from django_atlas.enums import EventType, LogStatus, ProductStatus
from django_atlas.models import ImportLog, IntegrationEvent, SourceProduct, SourceSettings
from django_atlas.schemas.contract import PriceStockUpdate, RawProduct
from django_atlas.services import import_service, log_service, lookup_provider
from django_atlas.signals import source_products_imported_signal
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "xml"
SAMPLE_FULL = (FIXTURE_DIR / "sample_full.xml").read_bytes()
SAMPLE_DELTA = (FIXTURE_DIR / "sample_delta.xml").read_bytes()


_FEED_CONFIG = {
    "feed_url": "https://example.com/feed.xml",
    "field_mapping": {
        "external_id": "./sku/text()",
        "name": "./name/text()",
        "cost": "./price/text()",
        "currency": "./price/@currency",
        "stock": "./stock/text()",
        "ean": "./ean/text()",
        "url": "./url/text()",
        "description": "./description/text()",
        "manufacturer": "./manufacturer/text()",
        "category": "./category/text()",
    },
}


def _mock_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.status_code = 200
    resp.iter_content = MagicMock(return_value=[content])
    resp.raise_for_status = MagicMock()
    return resp


def _sync_connector_mock() -> MagicMock:
    """MagicMock(spec=SyncConnector) with lifecycle hooks configured as real no-ops.

    A bare MagicMock(spec=SyncConnector) makes batch_size()/rate_limit_delay() return a
    truthy MagicMock instead of None, which silently coerces the batch size to 1 (via
    `__index__`) and triggers a spurious `time.sleep(1.0)` after every single-row batch.
    """
    connector = MagicMock(spec=SyncConnector)
    connector.batch_size.return_value = None
    connector.rate_limit_delay.return_value = None
    connector.should_retry.return_value = False
    return connector


def _patched_get(content):
    return patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(content))


# ============== FULL SYNC ==============


@pytest.mark.django_db
def test_full_sync_empty_feed():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    empty_xml = b'<?xml version="1.0" encoding="UTF-8"?><catalog></catalog>'
    with _patched_get(empty_xml):
        log = import_service.execute_feed(feed)
    assert log.status == LogStatus.SUCCESS.value
    assert log.total_count == 0
    assert log.new_count == 0


@pytest.mark.django_db
def test_full_sync_creates_five_products():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        log = import_service.execute_feed(feed)
    assert log.status == LogStatus.SUCCESS.value
    assert log.new_count == 5
    assert log.total_count == 5
    assert SourceProduct.objects.filter(source=feed.source).count() == 5


@pytest.mark.django_db
def test_full_sync_idempotent_unchanged():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
        log2 = import_service.execute_feed(feed)
    assert log2.unchanged_count == 5
    sp = SourceProduct.objects.filter(source=feed.source).first()
    # data_changed_at recorded on first sync; not bumped on second when hash unchanged
    initial_changed = sp.data_changed_at
    assert initial_changed is not None


@pytest.mark.django_db
def test_full_sync_modified_data_bumps_updated():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    modified = SAMPLE_FULL.replace(
        b"<description>Adjustable office chair with lumbar support</description>",
        b"<description>NEW DESC</description>",
    )
    with _patched_get(modified):
        log2 = import_service.execute_feed(feed)
    assert log2.updated_count == 1
    sp = SourceProduct.objects.get(source=feed.source, external_id="SKU-001")
    assert sp.data["description"] == "NEW DESC"


@pytest.mark.django_db
def test_full_sync_delisting_marks_rejected():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    # Drop SKU-005 from fixture
    reduced = SAMPLE_FULL.replace(
        SAMPLE_FULL[
            SAMPLE_FULL.find(b"<product>\n    <sku>SKU-005") : SAMPLE_FULL.find(
                b"</product>", SAMPLE_FULL.find(b"SKU-005")
            )
            + len(b"</product>")
        ],
        b"",
    )
    with _patched_get(reduced):
        log2 = import_service.execute_feed(feed)
    assert log2.delisted_count == 1
    sp = SourceProduct.objects.get(source=feed.source, external_id="SKU-005")
    assert sp.status == ProductStatus.REJECTED.value


@pytest.mark.django_db
def test_full_sync_pushed_product_delisted_event():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    sp = SourceProduct.objects.get(source=feed.source, external_id="SKU-001")
    sp.status = ProductStatus.PUSHED.value
    sp.save()
    reduced = SAMPLE_FULL.replace(b"<sku>SKU-001</sku>", b"<sku>SKU-001-changed</sku>")
    # also remove SKU-001 entirely so it is "missing" from feed
    fragment = SAMPLE_FULL[SAMPLE_FULL.find(b"<product>") : SAMPLE_FULL.find(b"</product>") + len(b"</product>")]
    reduced = SAMPLE_FULL.replace(fragment, b"", 1)
    with _patched_get(reduced):
        import_service.execute_feed(feed)
    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED.value  # NIE rusza
    assert IntegrationEvent.objects.filter(event_type="pushed_product_delisted").exists()


@pytest.mark.django_db
def test_full_sync_mass_delisting_event():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    # 10 pushed SP, 6 of them will be missing in feed
    for n in range(10):
        SourceProductFactory(source=source, feed=feed, external_id=f"PUSH-{n:03d}", status=ProductStatus.PUSHED.value)
    with _patched_get(SAMPLE_FULL):  # SAMPLE_FULL has SKU-001..005, none of PUSH-*
        import_service.execute_feed(feed)
    assert IntegrationEvent.objects.filter(event_type="mass_delisting").exists()


@pytest.mark.django_db
def test_full_sync_counts_pushed_delisted_separately():
    # pushed-product delistings increment `pushed_delisted_count` AND `mass_delisting_triggered`,
    # but NOT the legacy `delisted_count` (which keeps non-pushed semantics for backwards compat).
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    SourceProductFactory(source=source, feed=feed, external_id="PUSHED-GONE", status=ProductStatus.PUSHED.value)
    SourceProductFactory(source=source, feed=feed, external_id="NEW-GONE", status=ProductStatus.NEW.value)

    with _patched_get(SAMPLE_FULL):  # feed has SKU-001..005, neither of the staged SPs is present
        log = import_service.execute_feed(feed)

    assert log.pushed_delisted_count == 1, "pushed SP should be tracked separately"
    assert log.delisted_count == 1, "non-pushed SP keeps the legacy `delisted_count` semantics"
    # pushed_total=1 (the one still in PUSHED status — event only, no status flip), pushed_delisted=1 → 100% ratio → trip
    assert log.mass_delisting_triggered is True


@pytest.mark.django_db
def test_full_sync_persists_new_count_fields_to_import_log():
    # new ImportLog fields default to 0/False even when no delisting happens.
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        log = import_service.execute_feed(feed)
    assert log.pushed_delisted_count == 0
    assert log.mass_delisting_triggered is False
    # Ensure the response shape stays additive — old fields untouched.
    assert log.delisted_count == 0
    assert log.new_count == 5


@pytest.mark.django_db
def test_full_sync_ean_match_rename():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    SourceProductFactory(source=source, feed=feed, external_id="OLD-001", ean="5901234123457", name="legacy")
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    qs = SourceProduct.objects.filter(source=source, ean="5901234123457")
    assert qs.count() == 1  # no duplicate
    sp = qs.first()
    assert sp.external_id == "SKU-001"
    assert "OLD-001" in sp.external_id_history


@pytest.mark.django_db
def test_full_sync_ean_match_history_cap_20():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    history = [f"v{n:02d}" for n in range(1, 21)]
    SourceProductFactory(
        source=source, feed=feed, external_id="CURRENT", ean="5901234123457", external_id_history=history
    )
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    sp = SourceProduct.objects.get(source=source, ean="5901234123457")
    assert len(sp.external_id_history) == 20
    assert sp.external_id_history[-1] == "CURRENT"
    assert "v01" not in sp.external_id_history  # FIFO drop


@pytest.mark.django_db
def test_full_sync_bulk_batching_two_calls_for_1000():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)

    def _gen_raws():
        for i in range(1000):
            yield RawProduct(external_id=f"BIG-{i:04d}", name=f"P{i}", attributes={"i": i})

    with patch.object(SourceProduct.objects, "bulk_create", wraps=SourceProduct.objects.bulk_create) as bspy:
        connector = _sync_connector_mock()
        connector.is_async = False
        connector.fetch.return_value = _gen_raws()
        log = import_service.process_full_sync(feed, connector, log_service.start_import_log(feed, mode="full"))
    assert log["new_count"] == 1000
    assert bspy.call_count >= 2  # two batches of 500


@pytest.mark.django_db
def test_full_sync_hash_stability_reordered_keys():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    raw = RawProduct(external_id="R-1", name="N", attributes={"a": 1, "b": 2})
    sp1, _ = import_service.update_or_create_source_product(source, feed, raw)
    raw2 = RawProduct(external_id="R-1", name="N", attributes={"b": 2, "a": 1})
    sp2, _ = import_service.update_or_create_source_product(source, feed, raw2)
    assert sp1.data_hash == sp2.data_hash


@pytest.mark.django_db
def test_full_sync_data_changed_at_only_when_hash_changes():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    raw = RawProduct(external_id="R-1", name="N", attributes={"a": 1})
    sp, _ = import_service.update_or_create_source_product(source, feed, raw)
    first_changed = sp.data_changed_at
    sp_again, _ = import_service.update_or_create_source_product(source, feed, raw)
    assert sp_again.data_changed_at == first_changed


@pytest.mark.django_db
def test_full_sync_physical_changed_at_when_weight_changes():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    raw1 = RawProduct(external_id="R-1", name="N", attributes={"weight": "1.0"})
    sp1, _ = import_service.update_or_create_source_product(source, feed, raw1)
    raw2 = RawProduct(external_id="R-1", name="N", attributes={"weight": "1.5"})
    sp2, _ = import_service.update_or_create_source_product(source, feed, raw2)
    assert sp2.physical_changed_at is not None


@pytest.mark.django_db
def test_full_sync_last_synced_at_updates_each_run():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    sp = SourceProduct.objects.filter(source=feed.source).first()
    first_synced = sp.last_synced_at
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    sp.refresh_from_db()
    assert sp.last_synced_at >= first_synced


# ============== LOOKUP FRESHNESS ==============
# Full sync persists via bulk_create/bulk_update, which fire no post_save — lookup_provider's
# signal_specs never sees these rows. import_service._enqueue_lookup_refresh is the freshness
# path for that gap; these tests cover it directly (checkpoint BG-10 #12).


def _install_fake_lookup_tasks(monkeypatch) -> MagicMock:
    """Inject a fake `django_lookup.tasks.refresh_fingerprint` — a MagicMock we assert calls on."""
    fake_task = MagicMock()
    fake_pkg = types.ModuleType("django_lookup")
    fake_tasks = types.ModuleType("django_lookup.tasks")
    fake_tasks.refresh_fingerprint = fake_task
    monkeypatch.setitem(sys.modules, "django_lookup", fake_pkg)
    monkeypatch.setitem(sys.modules, "django_lookup.tasks", fake_tasks)
    return fake_task


@pytest.mark.django_db
def test_full_sync_enqueues_lookup_refresh_for_new_rows(monkeypatch):
    fake_task = _install_fake_lookup_tasks(monkeypatch)
    feed = FeedFactory(feed_config=_FEED_CONFIG)

    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)

    calls = fake_task.delay.call_args_list
    assert {call.args[0] for call in calls} == {lookup_provider.KIND}
    assert {call.args[1] for call in calls} == {f"{feed.source.idx}:SKU-00{i}" for i in range(1, 6)}


@pytest.mark.django_db
def test_full_sync_lookup_refresh_skips_unchanged_rows(monkeypatch):
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)  # first sync, no fake installed — must not raise

    fake_task = _install_fake_lookup_tasks(monkeypatch)
    with _patched_get(SAMPLE_FULL):  # identical feed -> every row "unchanged"
        import_service.execute_feed(feed)

    fake_task.delay.assert_not_called()


@pytest.mark.django_db
def test_full_sync_lookup_refresh_only_the_changed_row(monkeypatch):
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)

    fake_task = _install_fake_lookup_tasks(monkeypatch)
    modified = SAMPLE_FULL.replace(
        b"<description>Adjustable office chair with lumbar support</description>",
        b"<description>NEW DESC</description>",
    )
    with _patched_get(modified):
        import_service.execute_feed(feed)

    refs = {call.args[1] for call in fake_task.delay.call_args_list}
    assert refs == {f"{feed.source.idx}:SKU-001"}


@pytest.mark.django_db
def test_full_sync_survives_django_lookup_being_genuinely_absent(monkeypatch):
    """`sys.modules["django_lookup"] = None` is how Python itself represents "genuinely not
    installed" (ModuleNotFoundError) — distinct from this dev checkout's own RuntimeError
    ("on PYTHONPATH but not in INSTALLED_APPS"), which every other test in this file already
    exercises implicitly. Import must not break the pipeline either way."""
    monkeypatch.setitem(sys.modules, "django_lookup", None)
    feed = FeedFactory(feed_config=_FEED_CONFIG)

    with _patched_get(SAMPLE_FULL):
        log = import_service.execute_feed(feed)

    assert log.status == LogStatus.SUCCESS.value
    assert SourceProduct.objects.filter(source=feed.source).count() == 5


@pytest.mark.django_db
def test_full_sync_lookup_refresh_broker_failure_does_not_break_import(monkeypatch):
    fake_task = _install_fake_lookup_tasks(monkeypatch)
    fake_task.delay.side_effect = ConnectionError("broker unreachable")
    feed = FeedFactory(feed_config=_FEED_CONFIG)

    with _patched_get(SAMPLE_FULL):
        log = import_service.execute_feed(feed)

    assert log.status == LogStatus.SUCCESS.value
    assert SourceProduct.objects.filter(source=feed.source).count() == 5


# ============== DELTA SYNC ==============


@pytest.mark.django_db
def test_delta_sync_updates_cost_stock_currency():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    for ext_id, cost, stock in [("SKU-001", "129.00", 42), ("SKU-002", "499.00", 10), ("SKU-003", "89.50", 25)]:
        SourceProductFactory(
            source=source, feed=feed, external_id=ext_id, cost=Decimal(cost), stock=stock, currency="EUR"
        )
    with _patched_get(SAMPLE_DELTA):
        log = import_service.execute_feed(feed, mode="delta")
    sp = SourceProduct.objects.get(source=source, external_id="SKU-001")
    assert sp.cost == Decimal("119.00")
    assert sp.stock == 30
    assert log.status == LogStatus.SUCCESS.value


@pytest.mark.django_db
def test_delta_sync_unknown_external_id_emits_event():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    # No SP exists in DB → all 3 deltas are unknown → status='partial' (error_count>0)
    with _patched_get(SAMPLE_DELTA):
        log = import_service.execute_feed(feed, mode="delta")
    assert IntegrationEvent.objects.filter(event_type="unknown_external_id_in_delta").count() == 3
    assert log.error_count == 3
    assert log.status == LogStatus.PARTIAL.value


@pytest.mark.django_db
def test_delta_sync_unchanged_when_values_equal():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    SourceProductFactory(
        source=source, feed=feed, external_id="SKU-002", cost=Decimal("499.00"), stock=10, currency="EUR"
    )
    with _patched_get(SAMPLE_DELTA):
        log = import_service.execute_feed(feed, mode="delta")
    assert log.unchanged_count >= 1


@pytest.mark.django_db
def test_delta_sync_does_not_delist():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    SourceProductFactory(source=source, feed=feed, external_id="GHOST", status=ProductStatus.NEW.value)
    with _patched_get(SAMPLE_DELTA):
        log = import_service.execute_feed(feed, mode="delta")
    sp = SourceProduct.objects.get(source=source, external_id="GHOST")
    assert sp.status == ProductStatus.NEW.value  # not rejected
    assert log.delisted_count == 0


@pytest.mark.django_db
def test_delta_sync_physical_skipped_for_non_pushed_sp():
    """By design: physical update is applied ONLY to pushed SP (status pushed/pushed_pending_images).
    SP without real_product (still 'new'/'approved') keeps physical_changed_at untouched.
    """
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-001",
        cost=Decimal("129.00"),
        stock=42,
        status=ProductStatus.APPROVED.value,
    )
    sp_before = SourceProduct.objects.get(source=source, external_id="SKU-001")
    physical_before = sp_before.physical_changed_at
    upd = PriceStockUpdate(
        external_id="SKU-001", cost=Decimal("100"), stock=10, physical={"weight": "2.0", "ean": "9991234567890"}
    )
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)
    sp_after = SourceProduct.objects.get(source=source, external_id="SKU-001")
    assert sp_after.physical_changed_at == physical_before  # not bumped (no real_product)


@pytest.mark.django_db
def test_delta_sync_physical_updates_real_product_for_pushed_sp():
    """Pushed SP with real_product → delta physical fields update RealProduct.

    requires a primary SourceProductLink for the write to land (race detection).
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProductLink

    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-physupd", weight=Decimal("1.00"))
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-001",
        cost=Decimal("129.00"),
        stock=42,
        status=ProductStatus.PUSHED.value,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=source, is_primary=True, is_active=True)

    upd = PriceStockUpdate(external_id="SKU-001", physical={"weight": "2.50"})
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)

    rp.refresh_from_db()
    assert rp.weight == Decimal("2.50")
    sp.refresh_from_db()
    assert sp.physical_changed_at is not None
    assert IntegrationEvent.objects.filter(
        event_type=EventType.PHYSICAL_UPDATE_APPLIED.value, source_product=sp
    ).exists()


@pytest.mark.django_db
def test_delta_sync_physical_no_change_does_not_touch_real_product():
    """If physical values equal current RealProduct values, no save / no event."""
    from django_pim.models.real_product import RealProduct

    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-physeq", weight=Decimal("1.50"))
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-001",
        cost=Decimal("129.00"),
        stock=42,
        status=ProductStatus.PUSHED.value,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    physical_before = sp.physical_changed_at

    upd = PriceStockUpdate(external_id="SKU-001", physical={"weight": "1.50"})
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)

    sp.refresh_from_db()
    assert sp.physical_changed_at == physical_before
    assert not IntegrationEvent.objects.filter(
        event_type=EventType.PHYSICAL_UPDATE_APPLIED.value, source_product=sp
    ).exists()


@pytest.mark.django_db
def test_delta_sync_physical_for_pushed_pending_images_also_applies():
    """Status pushed_pending_images → physical update still applies (intermediate pushed state).

    requires a primary SourceProductLink for the write to land.
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProductLink

    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-physpp", weight=Decimal("1.00"))
    sp = SourceProductFactory(
        source=source, feed=feed, external_id="SKU-001", status=ProductStatus.PUSHED_PENDING_IMAGES.value
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=source, is_primary=True, is_active=True)

    upd = PriceStockUpdate(external_id="SKU-001", physical={"weight": "3.00"})
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)

    rp.refresh_from_db()
    assert rp.weight == Decimal("3.00")


@pytest.mark.django_db
def test_delta_sync_physical_shared_across_channels():
    """RealProduct is shared — multi-channel SP gets a single RealProduct update.

    requires a primary SourceProductLink for the write to land.
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProductLink

    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-physmc", weight=Decimal("1.00"))
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-001",
        status=ProductStatus.PUSHED.value,
        pushed_to_channel_idxs=["ch-a", "ch-b", "ch-c"],
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=source, is_primary=True, is_active=True)

    upd = PriceStockUpdate(external_id="SKU-001", physical={"weight": "5.55"})
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)

    rp.refresh_from_db()
    assert rp.weight == Decimal("5.55")
    # Single physical_update_applied event (one RealProduct write).
    assert (
        IntegrationEvent.objects.filter(event_type=EventType.PHYSICAL_UPDATE_APPLIED.value, source_product=sp).count()
        == 1
    )


@pytest.mark.django_db
def test_delta_sync_no_physical_in_update_only_cost_stock_changes():
    """If PriceStockUpdate.physical is None, only cost/stock/currency update."""
    from django_pim.models.real_product import RealProduct

    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-nophys", weight=Decimal("1.00"))
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-001",
        cost=Decimal("100"),
        stock=10,
        status=ProductStatus.PUSHED.value,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])

    upd = PriceStockUpdate(external_id="SKU-001", cost=Decimal("99"), stock=20)
    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")
    import_service.process_delta_sync(feed, connector, log)

    rp.refresh_from_db()
    assert rp.weight == Decimal("1.00")  # untouched
    sp.refresh_from_db()
    assert sp.cost == Decimal("99")
    assert sp.stock == 20


# ============== ASYNC DISPATCH ==============


@pytest.mark.django_db
def test_async_execute_dispatches_and_log_running():
    feed = FeedFactory(connector_kind="scraper", feed_config={"scraper_id": "s", "catalog_url": "https://example.com"})
    SourceSettings.load()
    with patch("django_atlas.connectors.scraper.current_app.send_task") as st:
        st.return_value = MagicMock(id="task-1")
        log = import_service.execute_feed(feed)
    assert log.status == LogStatus.RUNNING.value
    assert ImportLog.objects.filter(run_id=log.run_id).exists()
    st.assert_called_once()


@pytest.mark.django_db
def test_process_feed_results_finalizes_success():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    log = log_service.start_import_log(feed, mode="full")
    raws = [RawProduct(external_id=f"A-{n}", name=f"N{n}") for n in range(3)]
    finalized = import_service.process_feed_results(log.run_id, raws)
    assert finalized.status == LogStatus.SUCCESS.value
    assert finalized.new_count == 3


@pytest.mark.django_db
def test_process_feed_results_unknown_run_id_raises():
    with pytest.raises(ImportLog.DoesNotExist):
        import_service.process_feed_results(uuid4(), [])


# ============== ERRORS ==============


@pytest.mark.django_db
def test_execute_feed_connector_exception_marks_failed():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", side_effect=RuntimeError("boom")):
        log = import_service.execute_feed(feed)
    assert log.status == LogStatus.FAILED.value
    assert log.error_summary
    assert IntegrationEvent.objects.filter(event_type="feed_failed").exists()


@pytest.mark.django_db
def test_execute_feed_per_row_validation_error_increments_error_count():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    raws = [
        RawProduct(external_id="OK-1", name="ok")
        # Inject failure inside _build_or_update_sp via patched hash to throw on second
    ]

    bad = RawProduct(external_id="BAD-1", name="x")
    raws.append(bad)

    real_build = import_service._build_or_update_sp

    def _flaky(*args, **kwargs):
        if kwargs.get("raw").external_id == "BAD-1":
            raise ValueError("simulated row failure")
        return real_build(*args, **kwargs)

    connector = _sync_connector_mock()
    connector.is_async = False
    connector.fetch.return_value = iter(raws)
    log = log_service.start_import_log(feed, mode="full")
    with patch.object(import_service, "_build_or_update_sp", side_effect=_flaky):
        counts = import_service.process_full_sync(feed, connector, log)
    assert counts["error_count"] == 1
    assert counts["new_count"] == 1


@pytest.mark.django_db
def test_finalize_with_error_count_marks_partial():
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG, sync_mode="delta")
    with _patched_get(SAMPLE_DELTA):  # 3 unknown_external_id → partial
        log = import_service.execute_feed(feed, mode="delta")
    assert log.status == LogStatus.PARTIAL.value


@pytest.mark.django_db
def test_feed_last_sync_status_updated():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
    feed.refresh_from_db()
    assert feed.last_sync_status == LogStatus.SUCCESS.value


# ============== BULK / IDEMPOTENCY ==============


@pytest.mark.django_db
def test_re_execute_full_sync_unchanged():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    with _patched_get(SAMPLE_FULL):
        import_service.execute_feed(feed)
        log2 = import_service.execute_feed(feed)
    assert log2.unchanged_count == 5


@pytest.mark.django_db
def test_concurrency_unique_constraint_protects():
    """Statement-level: gdy próbujemy bulk_create z duplikatami → IntegrityError catched naturalnie.
    Tu po prostu sprawdzamy, że unique constraint działa."""
    source = SourceFactory()
    feed = FeedFactory(source=source, feed_config=_FEED_CONFIG)
    SourceProductFactory(source=source, feed=feed, external_id="DUP-1")
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        SourceProduct.objects.create(source=source, feed=feed, external_id="DUP-1", name="x")


# ============== SIGNALS ==============


@pytest.mark.django_db
def test_signal_emitted_after_successful_full_sync():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    received = []

    def _handler(sender, feed, import_log, **kwargs):  # noqa: ARG001
        received.append((feed.id, import_log.run_id))

    source_products_imported_signal.connect(_handler)
    try:
        with _patched_get(SAMPLE_FULL):
            import_service.execute_feed(feed)
    finally:
        source_products_imported_signal.disconnect(_handler)
    assert len(received) == 1
    assert received[0][0] == feed.id


@pytest.mark.django_db
def test_signal_not_emitted_after_failure():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    received = []

    def _handler(sender, **kwargs):  # noqa: ARG001
        received.append(1)

    source_products_imported_signal.connect(_handler)
    try:
        with patch("django_atlas.security.url_guard.requests.get", side_effect=RuntimeError("boom")):
            import_service.execute_feed(feed)
    finally:
        source_products_imported_signal.disconnect(_handler)
    assert received == []


# ============== VALIDATION FLOW ==============


@pytest.mark.django_db
def test_execute_feed_with_invalid_stored_config_marks_failed():
    feed = FeedFactory(feed_config=_FEED_CONFIG)
    feed.feed_config = {}  # corrupt config bypassing service validation
    feed.save(update_fields=["feed_config", "modified_at"])
    log = import_service.execute_feed(feed)
    assert log.status == LogStatus.FAILED.value
