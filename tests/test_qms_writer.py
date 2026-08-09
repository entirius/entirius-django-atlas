# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for qms_writer (soft dep on django_qms; we mock the warehouse_service)."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from django_atlas.enums import EventType, ProductStatus
from django_atlas.models import IntegrationEvent, SourceSettings
from django_atlas.services import qms_writer
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


class _FakeWarehouseDoesNotExist(Exception):
    """Stand-in for django_qms.models.Warehouse.DoesNotExist."""


class _FakeWarehouseManager:
    """Mimics Warehouse.objects.get(code=...). Returned object is the same fake the test owns."""

    def __init__(self, lookup):
        self._lookup = lookup

    def get(self, *, code):
        warehouse = self._lookup(code)
        if warehouse is None:
            raise _FakeWarehouseDoesNotExist(f"Warehouse(code={code!r}) does not exist")
        return warehouse


class _FakeWarehouseModel:
    """Stand-in for django_qms.models.Warehouse — exposes .objects and .DoesNotExist."""

    DoesNotExist = _FakeWarehouseDoesNotExist

    def __init__(self, lookup):
        self.objects = _FakeWarehouseManager(lookup)


def _install_fake_qms(monkeypatch, mock_service, *, warehouse_lookup=None):
    """Inject a fake `django_qms.services.warehouse_service` AND `django_qms.models.Warehouse`.

    qms_writer.write_stock imports both `Warehouse` and `warehouse_service` from
    django_qms inside the same try-block. Mocking only `services` lets the
    `from django_qms.models import Warehouse` line raise ImportError, which is
    swallowed by the outer except and produces a QMS_WRITE_FAILED event instead
    of exercising the happy path. Tests must inject both.

    `warehouse_lookup(code)` returns the Warehouse object (any sentinel; we only
    assert identity) or `None` to trigger Warehouse.DoesNotExist. Defaults to a
    stub that returns a fresh sentinel object for every code.
    """
    if warehouse_lookup is None:
        sentinel = object()

        def warehouse_lookup(code):  # noqa: ARG001 — same sentinel for every lookup
            return sentinel

    fake_pkg = types.ModuleType("django_qms")
    fake_services = types.ModuleType("django_qms.services")
    fake_services.warehouse_service = mock_service
    fake_models = types.ModuleType("django_qms.models")
    fake_models.Warehouse = _FakeWarehouseModel(warehouse_lookup)
    monkeypatch.setitem(sys.modules, "django_qms", fake_pkg)
    monkeypatch.setitem(sys.modules, "django_qms.services", fake_services)
    monkeypatch.setitem(sys.modules, "django_qms.models", fake_models)
    qms_writer._qms_available.cache_clear()
    return fake_models.Warehouse  # tests assert call args by identity


def _uninstall_fake_qms(monkeypatch):
    """Remove the fake qms package so _qms_available() returns False."""
    qms_writer._qms_available.cache_clear()
    for name in ("django_qms.models", "django_qms.services", "django_qms"):
        if name in sys.modules:
            sys.modules.pop(name)
    qms_writer._qms_available.cache_clear()


@pytest.fixture(autouse=True)
def _ensure_qms_unloaded():
    """Each test starts with _qms_available() returning False (unless test installs fake)."""
    qms_writer._qms_available.cache_clear()
    yield
    qms_writer._qms_available.cache_clear()


def _build_pushed_sp_with_rp(source, sku="qms-sku"):
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku=sku)
    sp = SourceProductFactory(source=source, status=ProductStatus.PUSHED.value, stock=10)
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    return sp


def test_qms_unavailable_silent_skip(caplog):
    """Perf #9: QMS missing → silent skip, no per-SP event.

    Operators see ONE startup log warning (logged via _qms_available cache); per-SP
    events were noise (50k/day on a 50k-SP daily delta with QMS uninstalled).
    """
    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-1")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    # No event for the missing-QMS path — silent skip.
    assert not IntegrationEvent.objects.filter(
        event_type=EventType.QMS_NOT_INSTALLED_SKIPPED.value, source_product=sp
    ).exists()


def test_qms_warehouse_not_configured_warns(monkeypatch):
    _install_fake_qms(monkeypatch, MagicMock())
    sup = SourceFactory(target_warehouse_code=None)
    sp = _build_pushed_sp_with_rp(sup, "qms-2")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    assert IntegrationEvent.objects.filter(event_type=EventType.QMS_WAREHOUSE_NOT_CONFIGURED.value, source=sup).exists()


def test_delta_killswitch_skips_silently(monkeypatch):
    _install_fake_qms(monkeypatch, MagicMock())
    settings = SourceSettings.load()
    settings.delta_sync_enabled = False
    settings.save()

    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-3")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="delta")

    # No event of any kind for this SP — silent skip.
    assert not IntegrationEvent.objects.filter(source_product=sp).exists()


def test_push_context_ignores_delta_killswitch(monkeypatch):
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    settings = SourceSettings.load()
    settings.delta_sync_enabled = False
    settings.save()

    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-4")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    mock_service.bulk_upsert_stock.assert_called_once()


def test_dispatch_from_shipping_time_h_wins(monkeypatch):
    """Per-product shipping_time_h is threaded into the QMS item and beats lead_time_days."""
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1", lead_time_days=3)
    sp = _build_pushed_sp_with_rp(sup, "qms-disp-1")
    sp.data = {"shipping_time_h": 24}
    sp.save(update_fields=["data"])
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    item = mock_service.bulk_upsert_stock.call_args.kwargs["items"][0]
    assert item["dispatch_time_h"] == 24


def test_dispatch_falls_back_to_lead_time_days(monkeypatch):
    """No shipping_time_h → lead_time_days * 24."""
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1", lead_time_days=3)
    sp = _build_pushed_sp_with_rp(sup, "qms-disp-2")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    item = mock_service.bulk_upsert_stock.call_args.kwargs["items"][0]
    assert item["dispatch_time_h"] == 72


def test_dispatch_absent_when_no_source(monkeypatch):
    """Neither shipping_time_h nor lead_time_days → key omitted so QMS keeps any override."""
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1", lead_time_days=None)
    sp = _build_pushed_sp_with_rp(sup, "qms-disp-3")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    item = mock_service.bulk_upsert_stock.call_args.kwargs["items"][0]
    assert "dispatch_time_h" not in item


def test_no_real_product_warns(monkeypatch):
    _install_fake_qms(monkeypatch, MagicMock())
    sup = SourceFactory(target_warehouse_code="WH1")
    sp = SourceProductFactory(source=sup, status=ProductStatus.PUSHED.value)
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    assert IntegrationEvent.objects.filter(event_type=EventType.QMS_NO_REAL_PRODUCT.value, source_product=sp).exists()


def test_happy_path_calls_bulk_upsert_and_records_event(monkeypatch):
    mock_service = MagicMock()
    expected_warehouse = object()
    _install_fake_qms(monkeypatch, mock_service, warehouse_lookup=lambda code: expected_warehouse)
    sup = SourceFactory(target_warehouse_code="WH1", qty_subtract=0, qty_minimum=0)
    sp = _build_pushed_sp_with_rp(sup, "qms-happy")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")

    # Workbook §3 Fix #5: warehouse passed as a Warehouse object (not warehouse_code string)
    # AND allow_integration=True so the source feed write is accepted as a non-manual update.
    mock_service.bulk_upsert_stock.assert_called_once_with(
        warehouse=expected_warehouse, items=[{"sku": "qms-happy", "quantity": 10}], allow_integration=True
    )
    assert IntegrationEvent.objects.filter(event_type=EventType.STOCK_UPDATED.value, source_product=sp).exists()


def test_quantity_passed_through_modifier(monkeypatch):
    mock_service = MagicMock()
    expected_warehouse = object()
    _install_fake_qms(monkeypatch, mock_service, warehouse_lookup=lambda code: expected_warehouse)
    sup = SourceFactory(target_warehouse_code="WH1", qty_subtract=1, qty_minimum=0)
    sp = _build_pushed_sp_with_rp(sup, "qms-mod")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    mock_service.bulk_upsert_stock.assert_called_once_with(
        warehouse=expected_warehouse,
        items=[{"sku": "qms-mod", "quantity": 9}],  # 10 - 1
        allow_integration=True,
    )


def test_multi_channel_single_call(monkeypatch):
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-mc")
    qms_writer.write_stock(sp, sup, ["ch-1", "ch-2", "ch-3"], context="push")
    # Warehouse is per-source (not per-channel) → 1 call total.
    assert mock_service.bulk_upsert_stock.call_count == 1


def test_qms_exception_records_warning_no_reraise(monkeypatch):
    mock_service = MagicMock()
    mock_service.bulk_upsert_stock.side_effect = RuntimeError("qms down")
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-fail")
    # Must not raise — soft dep contract.
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")
    assert IntegrationEvent.objects.filter(event_type=EventType.QMS_WRITE_FAILED.value, source_product=sp).exists()


def test_settings_runtime_change_invalidates_cache(monkeypatch):
    """delta_sync_enabled toggle must take effect within the same test (cache invalidation)."""
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1")
    sp = _build_pushed_sp_with_rp(sup, "qms-toggle")

    # Enabled — write goes through.
    settings = SourceSettings.load()
    settings.delta_sync_enabled = True
    settings.save()
    qms_writer.write_stock(sp, sup, ["ch-1"], context="delta")
    assert mock_service.bulk_upsert_stock.call_count == 1

    # Disabled — silent skip.
    settings.delta_sync_enabled = False
    settings.save()
    qms_writer.write_stock(sp, sup, ["ch-1"], context="delta")
    assert mock_service.bulk_upsert_stock.call_count == 1


def test_warehouse_looked_up_by_source_target_code(monkeypatch):
    """qms_writer must call Warehouse.objects.get(code=source.target_warehouse_code).

    Regression guard for §3 Fix #5: previously the code passed `warehouse_code=` to
    bulk_upsert_stock; the new contract requires a Warehouse model lookup *here*,
    not at the qms service boundary.
    """
    mock_service = MagicMock()
    looked_up_codes: list[str] = []
    warehouse_obj = object()

    def lookup(code):
        looked_up_codes.append(code)
        return warehouse_obj

    _install_fake_qms(monkeypatch, mock_service, warehouse_lookup=lookup)
    sup = SourceFactory(target_warehouse_code="WH-LOOKUP")
    sp = _build_pushed_sp_with_rp(sup, "qms-lookup")
    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")

    assert looked_up_codes == ["WH-LOOKUP"]
    # And the looked-up object is the one handed to bulk_upsert_stock.
    call_kwargs = mock_service.bulk_upsert_stock.call_args.kwargs
    assert call_kwargs["warehouse"] is warehouse_obj


def test_warehouse_missing_records_write_failed_no_reraise(monkeypatch):
    """Warehouse.DoesNotExist must NOT crash the source flow — soft dep contract.

    The lookup raising DoesNotExist falls through the same `except Exception` as
    other qms-side errors, producing a QMS_WRITE_FAILED event with the missing
    warehouse code in `details.error`.
    """
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service, warehouse_lookup=lambda code: None)
    sup = SourceFactory(target_warehouse_code="WH-MISSING")
    sp = _build_pushed_sp_with_rp(sup, "qms-missing-warehouse")

    qms_writer.write_stock(sp, sup, ["ch-1"], context="push")

    mock_service.bulk_upsert_stock.assert_not_called()
    event = IntegrationEvent.objects.filter(event_type=EventType.QMS_WRITE_FAILED.value, source_product=sp).first()
    assert event is not None
    assert "WH-MISSING" in event.details.get("error", "")


def test_allow_integration_true_always_passed(monkeypatch):
    """The allow_integration kwarg must always be True for source feed writes.

    django-qms defaults `allow_integration=False`. Source feeds ARE the
    integration source, so leaving the default would have qms reject every
    stock change as if it were a manual override.
    """
    mock_service = MagicMock()
    _install_fake_qms(monkeypatch, mock_service)
    sup = SourceFactory(target_warehouse_code="WH1")

    # Two different contexts should both pass allow_integration=True.
    for sku, context in [("qms-allow-push", "push"), ("qms-allow-delta", "delta")]:
        if context == "delta":
            settings = SourceSettings.load()
            settings.delta_sync_enabled = True
            settings.save()
        sp = _build_pushed_sp_with_rp(sup, sku)
        qms_writer.write_stock(sp, sup, ["ch-1"], context=context)

    for call in mock_service.bulk_upsert_stock.call_args_list:
        assert call.kwargs.get("allow_integration") is True
