# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Signal definitions for django_atlas.

All signals follow Django dispatcher idioms (`Signal()`, `connect`/`send`).
Receivers MUST be idempotent — signals may fire repeatedly on retries
(Celery autoretry, delta sync re-run, force re-push). The same
(source_product, channel_idx) pair may receive >1 signal in a short window.

Stage 4 wires push handlers (image dispatch via `transaction.on_commit`).
Stage 5 wires pricemanager_writer (logs cost + emits event).
Stage 6 wires the auto-push handler.

Killswitches: `signals.killswitch.suppress_source_signals()` (thread-local
context) and `SourceSettings.auto_push_enabled` (DB-level, cached 60s).
"""

from django.dispatch import Signal

# Sent by import_service.execute_feed after a successful import finalization
# (full sync end OR async scraper-callback finalize).
# providing_args:
#   feed: SourceFeed instance
#   import_log: ImportLog instance (status='success' or 'partial')
# Stage 6 connects the auto-push handler (transaction.on_commit).
source_products_imported_signal = Signal()


# Emitted by pricemanager_writer / import_service after a SourceProduct cost
# is recorded for a target channel. Decoupled from the future Price Handling
# layer (decision #5 — pricemanager DEFER in MVP).
# providing_args:
#   source_product: SourceProduct instance
#   channel_idx: str (PIM Channel.idx the cost applies to)
#   cost: Decimal
#   currency: str (ISO 4217, e.g. "EUR")
# Receivers MUST be idempotent — same (source_product, channel_idx) may
# fire repeatedly during delta sync retry, full re-import, or force re-push.
cost_updated_signal = Signal()


# Emitted by push_service after a SourceProduct is pushed to a PIM channel
# (status transitions to pushed_pending_images or pushed). One emission per
# (source_product, channel_idx) pair per push.
# providing_args:
#   source_product: SourceProduct instance
#   real_product_sku: str
#   channel_idx: str
# Stage 4 wires the image-dispatch handler (per-channel image download task).
source_product_pushed_signal = Signal()


# emitted by primary_strategy_service.apply_primary_switch every time
# the auto-primary (or manual override / emergency / cron) flips is_primary on a
# new link for a RealProduct.
# providing_args:
#   real_product_sku: str
#   from_source_idx: str | None  (None when no prior primary)
#   to_source_idx: str
#   reason: str  (PrimarySkipReason.NONE.value when applied; "manual_override"
#                 / "emergency" / "auto" identifying the trigger source)
#   source: str  (ChangeLogSource value driving the audit row)
# No subscribers in sources/pricemanager yet — the cost subscriber reads
# is_primary via ORM, not via signal. Signal exists so downstream services
# (future Slack notifier, search reindex, cache invalidation) can hook in
# without modifying primary_strategy_service. Receivers MUST be idempotent
# (transaction.on_commit wrappers in the service make multiple emissions
# possible on retry).
primary_switched_signal = Signal()


# emitted by
# `observation_service.record_observation` after a new (append-only) Observation
# row is created.
# providing_args:
#   source: Source instance
#   sku: str
#   kind: str (SourceKind value, denormalized from source.kind)
#   value: dict (canonical shape per kind — see models/observation.py)
# No subscriber in v1 — reserved for a future reactive autopilot. A later stage wires
# the feed pipeline that calls `record_observation` per-row; this module only
# provides the write choke point + signal.
observation_recorded_signal = Signal()
