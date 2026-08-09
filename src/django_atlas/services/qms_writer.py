# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""QMS stock writer — soft dependency on django_qms.

Decision #6: SOFT — `try/except ImportError`. Without QMS installed, stock writes
are skipped (info event), source flow continues. Stock write target is
`Source.target_warehouse_code` (FK-string to qms.Warehouse.code).
"""

import functools
import logging
from typing import Any

from django_atlas.enums import EventSeverity, EventType
from django_atlas.models import Source, SourceProduct, SourceSettings
from django_atlas.services import event_service, modifier_service

logger = logging.getLogger(__name__)

_UNRESOLVED = object()


def resolve_warehouse(source: Source) -> Any:
    """Resolve `source.target_warehouse_code` once for reuse across a batch of writes.

    Returns None when QMS is unavailable, unconfigured, or the code has no matching
    Warehouse — callers pass the result straight into `write_stock(warehouse=...)`.
    """
    if not _qms_available() or not source.target_warehouse_code:
        return None
    from django_qms.models import Warehouse

    return Warehouse.objects.filter(code=source.target_warehouse_code).first()


@functools.cache
def _qms_available() -> bool:
    """Cached check — django_qms.services.warehouse_service importable.

    Logs ONE warning per process lifetime when QMS is absent (perf #9).
    """
    try:
        from django_qms.services import warehouse_service  # noqa: F401

        return True
    except (ImportError, RuntimeError):
        # side-fix: RuntimeError is raised when django_qms
        # is on PYTHONPATH but NOT in INSTALLED_APPS — common in multi-repo dev checkouts where every
        # repo is mounted but the test config only installs a subset. ImportError alone
        # leaks 58 baseline failures into the sources test suite.
        logger.warning("django_qms not installed — source stock writes will be skipped for this process lifetime")
        return False


def _resolve_dispatch_hours(sp: SourceProduct, source: Source) -> int | None:
    """Dispatch promise in hours: per-product `shipping_time_h` (SourceProduct.data) else
    `Source.lead_time_days * 24`. None when neither is set (QMS keeps any existing override).
    """
    raw = sp.data.get("shipping_time_h") if sp.data else None
    if raw is not None:
        try:
            return max(0, int(raw))  # clamp — a bad/negative feed value must not hit a PositiveIntegerField
        except (TypeError, ValueError):
            pass
    if source.lead_time_days:
        return max(0, source.lead_time_days * 24)
    return None


def write_stock(
    sp: SourceProduct,
    source: Source,
    channel_idxs: list[str],
    context: str = "push",
    *,
    settings: SourceSettings | None = None,
    warehouse: Any = _UNRESOLVED,
) -> None:
    """Write stock for an SP to QMS Warehouse via warehouse_service.bulk_upsert_stock.

    Soft path — never re-raises when QMS is missing or fails. Source flow continues.

    Args:
        sp: SourceProduct (must have real_product FK to derive SKU).
        source: Source owning the stock (target_warehouse_code source).
        channel_idxs: included for symmetry with pricemanager_writer; QMS warehouse
            is per-source (not per-channel), so this is informational only.
        context: 'push' (always writes) or 'delta' (respects delta_sync_enabled killswitch).
        settings: pre-loaded `SourceSettings.load()` for batch callers — avoids a per-row
            query. Defaults to loading it here when not supplied.
        warehouse: pre-resolved `resolve_warehouse(source)` for batch callers — avoids a
            per-row query. Defaults to resolving it here when not supplied.
    """
    if not _qms_available():
        # Perf #9: silently skip per-SP. The startup log warning (in _qms_available)
        # is the operator signal; emitting one event per SP × delta-run = noise.
        return
    if not source.target_warehouse_code:
        event_service.record(
            event_type=EventType.QMS_WAREHOUSE_NOT_CONFIGURED.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"Source '{source.idx}' has no target_warehouse_code, stock write skipped",
        )
        return
    if context == "delta":
        settings = settings if settings is not None else SourceSettings.load()
        if not settings.delta_sync_enabled:
            return  # silent skip — operator-toggled killswitch
    if sp.real_product_id is None:
        event_service.record(
            event_type=EventType.QMS_NO_REAL_PRODUCT.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"SP {sp.id} has no real_product FK, cannot write stock",
        )
        return

    quantity = modifier_service.apply_qty_modifiers(sp.stock, source)
    dispatch_time_h = _resolve_dispatch_hours(sp, source)

    try:
        from django_qms.models import Warehouse
        from django_qms.services import warehouse_service

        resolved_warehouse = (
            Warehouse.objects.get(code=source.target_warehouse_code) if warehouse is _UNRESOLVED else warehouse
        )
        if resolved_warehouse is None:
            raise Warehouse.DoesNotExist(f"No Warehouse with code={source.target_warehouse_code!r}")
        item = {"sku": sp.real_product.sku, "quantity": quantity}
        if dispatch_time_h is not None:
            item["dispatch_time_h"] = dispatch_time_h
        warehouse_service.bulk_upsert_stock(warehouse=resolved_warehouse, items=[item], allow_integration=True)
        event_service.record(
            event_type=EventType.STOCK_UPDATED.value,
            severity=EventSeverity.INFO.value,
            source=source,
            source_product=sp,
            message=f"Stock updated for sku={sp.real_product.sku} qty={quantity}",
            details={
                "sku": sp.real_product.sku,
                "warehouse_code": source.target_warehouse_code,
                "quantity": quantity,
                "raw_stock": sp.stock,
                "dispatch_time_h": dispatch_time_h,
                "context": context,
                "channel_idxs": list(channel_idxs),
            },
        )
    except Exception as exc:  # noqa: BLE001 — soft dep, never re-raise
        event_service.record(
            event_type=EventType.QMS_WRITE_FAILED.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"QMS write failed: {exc}",
            details={"context": context, "error": str(exc)},
        )
