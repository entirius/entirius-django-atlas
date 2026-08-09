# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Pricemanager-side cost logger — DEFER to future Price Handling layer (decision #5).

CRITICAL: this file MUST NOT import django_pricemanager. The source module logs cost
deltas as IntegrationEvents and emits `cost_updated_signal`. A future Price Handling
layer subscribes to that signal — source doesn't know or care what it does with it.

Decision #35: bulk volume scenarios (50k SP × 5 channels delta) use `event_service.record_bulk`
to issue 1 batch INSERT instead of N×INSERT.
"""

from django_atlas.enums import EventSeverity, EventType, SourceKind
from django_atlas.models import Source, SourceProduct, SourceSettings
from django_atlas.services import event_service
from django_atlas.signals import cost_updated_signal


def log_cost(
    sp: SourceProduct,
    source: Source,
    channel_idxs: list[str],
    context: str = "push",
    *,
    settings: SourceSettings | None = None,
) -> None:
    """Log a cost change per channel and emit `cost_updated_signal` per channel.

    - 'push' context always runs.
    - 'delta' context respects `SourceSettings.delta_sync_enabled` killswitch (silent skip).
    - Non-procurement source (monitoring/enrichment) → silent skip. Cost is a procurement-only
      concept; this is an explicit kind gate in addition to the real_product_id gate below —
      a nullable `cost` field alone is not sufficient (a monitoring source could theoretically
      still carry a stray value).
    - Missing cost / currency / real_product → warning event, skip.

    Bulk insert via event_service.record_bulk for the IntegrationEvents (decision #35).
    Signal emit stays per-channel (receivers expect discrete events).

    `settings`: pre-loaded `SourceSettings.load()` for batch callers — avoids a per-row query.
    """
    if context == "delta":
        settings = settings if settings is not None else SourceSettings.load()
        if not settings.delta_sync_enabled:
            return
    if source.kind != SourceKind.PROCUREMENT.value:
        return
    if sp.cost is None:
        event_service.record(
            event_type=EventType.COST_MISSING.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"SP {sp.id} has no cost, log skipped",
        )
        return
    if not sp.currency:
        event_service.record(
            event_type=EventType.CURRENCY_MISSING.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"SP {sp.id} has no currency, log skipped",
        )
        return
    if sp.real_product_id is None:
        event_service.record(
            event_type=EventType.COST_MISSING.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            source_product=sp,
            message=f"SP {sp.id} has no real_product FK, cost log skipped",
        )
        return

    sku = sp.real_product.sku
    cost_str = str(sp.cost)
    events_batch = [
        {
            "event_type": EventType.COST_UPDATED.value,
            "severity": EventSeverity.INFO.value,
            "source": source,
            "source_product": sp,
            "message": f"cost {cost_str} {sp.currency} for sku {sku} channel {channel_idx}",
            "details": {
                "source_idx": source.idx,
                "sku": sku,
                "cost": cost_str,
                "currency": sp.currency,
                "channel_idx": channel_idx,
                "context": context,
            },
        }
        for channel_idx in channel_idxs
    ]
    if events_batch:
        event_service.record_bulk(events_batch)

    for channel_idx in channel_idxs:
        cost_updated_signal.send(
            sender=type(sp), source_product=sp, channel_idx=channel_idx, cost=sp.cost, currency=sp.currency
        )
