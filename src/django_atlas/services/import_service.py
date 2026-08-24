# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import hashlib
import json
import logging
import time
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from decimal import Decimal
from itertools import islice
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from django_atlas.connectors.base import BaseConnector
from django_atlas.enums import (
    PUSHED_STATUSES,
    ChangeLogSource,
    EventSeverity,
    EventType,
    LogStatus,
    ProductStatus,
    SourceKind,
)
from django_atlas.models import ImportLog, Source, SourceFeed, SourceProduct, SourceProductLink, SourceSettings
from django_atlas.schemas.contract import PriceStockUpdate, RawProduct
from django_atlas.services import (
    audit_service,
    connector_registry,
    event_service,
    log_service,
    lookup_provider,
    observation_service,
)
from django_atlas.signals import source_products_imported_signal

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500
_HISTORY_CAP = 20
_PHYSICAL_KEYS = {"weight", "ean", "width", "height", "deep"}
_NON_PUSHED_DELIST_TARGETS = {ProductStatus.NEW.value, ProductStatus.QUEUED.value, ProductStatus.APPROVED.value}
_AUDIT_TRACKED_SCALAR_FIELDS = ("name", "cost", "currency", "stock", "ean")

# physical race detection outcomes — returned by _apply_physical_update_to_real_product
# so process_delta_sync can bucket counts without re-querying.
PHYSICAL_OUTCOME_NOOP = "noop"
PHYSICAL_OUTCOME_APPLIED = "applied"
PHYSICAL_OUTCOME_SKIPPED_NON_PRIMARY = "skipped_non_primary"
PHYSICAL_OUTCOME_OVERWRITTEN = "overwritten"


def _json_safe(value: Any) -> Any:
    """Convert Decimals to strings so JSONField stores stable shape across drivers."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _snapshot_for_audit(sp: SourceProduct | None) -> dict[str, Any] | None:
    """Capture mutable fields BEFORE `_build_or_update_sp` rewrites them in place."""
    if sp is None:
        return None
    snap: dict[str, Any] = {field: getattr(sp, field) for field in _AUDIT_TRACKED_SCALAR_FIELDS}
    snap["image_urls"] = list(sp.image_urls or [])
    snap["data"] = dict(sp.data or {})
    return snap


def _build_full_sync_audit_drafts(prev: dict[str, Any], sp: SourceProduct) -> list[dict[str, Any]]:
    """Diff pre-mutation snapshot against post-mutation SP — per-field drafts.

    Strategy (workbook §6.6 Q1 — per-key for `data`, whole-list for `image_urls`):
    one draft per changed scalar, one draft per added/removed/changed `data.{key}`,
    one draft with full before/after lists for `image_urls`.
    """
    drafts: list[dict[str, Any]] = []
    common = {"source_product": sp, "source": ChangeLogSource.FULL_SYNC.value, "applied_to_pim": False}
    for field in _AUDIT_TRACKED_SCALAR_FIELDS:
        prev_val = prev.get(field)
        new_val = getattr(sp, field)
        if prev_val != new_val:
            drafts.append({**common, "field_path": field, "before": _json_safe(prev_val), "after": _json_safe(new_val)})
    prev_imgs = prev.get("image_urls") or []
    new_imgs = list(sp.image_urls or [])
    if prev_imgs != new_imgs:
        drafts.append({**common, "field_path": "image_urls", "before": prev_imgs, "after": new_imgs})
    prev_data = prev.get("data") or {}
    new_data = sp.data or {}
    for key in sorted(set(prev_data) | set(new_data)):
        before = prev_data.get(key)
        after = new_data.get(key)
        if before != after:
            drafts.append(
                {**common, "field_path": f"data.{key}", "before": _json_safe(before), "after": _json_safe(after)}
            )
    return drafts


def _flush_audit_drafts(drafts: list[dict[str, Any]], *, context: str) -> None:
    """Out-of-band: audit log failure NEVER crashes the data path."""
    if not drafts:
        return
    try:
        audit_service.log_changes_bulk(drafts)
    except Exception:  # noqa: BLE001 — audit must be best-effort
        logger.warning("audit_service.log_changes_bulk failed in %s", context, exc_info=True)


def _lookup_ref(source: Source, external_id: str) -> str:
    """Ref format `lookup_provider.ref_for` owns — delegate instead of re-implementing it.

    Built from a bare `external_id` (not a `SourceProduct` instance) rather than `lookup_provider.
    ref_for(sp)` directly: `sp.source` is uncached for existing rows and would cost a query per
    SourceProduct, and a rename's pre-rename ref has no row to build one from at all (the row's own
    `external_id` has already been overwritten by `_build_or_update_sp`). Constructing a transient,
    unsaved `SourceProduct(source=source, external_id=external_id)` costs no query — passing a model
    instance for a FK at construction time caches it — so this covers both cases for free.
    """
    return lookup_provider.ref_for(SourceProduct(source=source, external_id=external_id))


def _enqueue_lookup_refresh(refs: list[str]) -> None:
    """Best-effort fingerprint refresh for source products a bulk write skipped signals for.

    `bulk_create`/`bulk_update` fire no `post_save`, so django-lookup's freshness wiring
    (`lookup_provider.signal_specs`) never sees these rows — full sync would otherwise leave
    imported rows invisible to the lookup candidate pool until an operator runs
    `lookup_backfill`/`lookup_reconcile`. Soft dependency, same posture as
    `django_lookup.signals._enqueue`: import guarded so atlas keeps working without django-lookup
    installed, and broker failures are swallowed — a catalog write must never fail because the
    lookup queue is unreachable, and a dropped enqueue is recoverable with `lookup_reconcile`.

    Publishes in chunks of `REFRESH_TASK_BATCH` instead of one `.delay()` per ref: a first full sync
    of a large feed changes every row, and one AMQP publish (and one worker task) per row would flood
    the `lookup` queue shared with image embedding. `django_lookup.tasks.refresh_fingerprints` is the
    batched twin of the single-ref task — same idempotent semantics, a thin loop underneath.
    """
    if not refs:
        return
    try:
        from django_lookup.constants import REFRESH_TASK_BATCH
        from django_lookup.tasks import refresh_fingerprints
    except (ImportError, RuntimeError):
        # RuntimeError alongside ImportError matches qms_writer._qms_available's own
        # precedent: Django raises RuntimeError (not ImportError) when a module is on
        # PYTHONPATH but not in INSTALLED_APPS — the common multi-repo dev-checkout shape.
        return
    for chunk in _batched(iter(refs), REFRESH_TASK_BATCH):
        try:
            refresh_fingerprints.delay(lookup_provider.KIND, chunk)
        except Exception:  # noqa: BLE001 — broker errors must not break the import pipeline
            logger.warning(
                "lookup: could not enqueue refresh for %d refs — run lookup_reconcile", len(chunk), exc_info=True
            )


def _hash_attributes(attributes: dict[str, Any]) -> str:
    payload = json.dumps(attributes, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 — non-crypto hash


def _kind_routed_fields(kind: str, raw: RawProduct) -> dict[str, Any]:
    """Route RawProduct's price/attributes onto the semantic field for `kind`.

    procurement -> cost; monitoring -> observed_price; enrichment -> signals. The other two
    fields are always cleared so a kind change on the Source never leaves a stale value behind.
    """
    if kind == SourceKind.MONITORING.value:
        return {"cost": None, "observed_price": raw.cost, "signals": None}
    if kind == SourceKind.ENRICHMENT.value:
        return {"cost": None, "observed_price": None, "signals": dict(raw.attributes)}
    return {"cost": raw.cost, "observed_price": None, "signals": None}


def _record_observation_for_sp(source: Source, sp: SourceProduct, *, raw: dict[str, Any] | None = None) -> None:
    """Append-only Observation write for matched monitoring/enrichment SPs.

    Unmatched SPs (no `real_product` FK yet) stay in the review-list without an
    observation — matching happens out-of-band (manual link / EAN auto-link), not here.
    Best-effort: never breaks the import pipeline (matches `_flush_audit_drafts` posture).
    """
    if not sp.real_product_id:
        return
    if source.kind == SourceKind.MONITORING.value:
        if sp.observed_price is None:
            return
        value: dict[str, Any] = {
            "price": str(sp.observed_price),
            "currency": sp.currency or source.default_currency.code,
            "stock": sp.stock,
        }
    elif source.kind == SourceKind.ENRICHMENT.value:
        if not sp.signals:
            return
        value = {"signals": sp.signals}
    else:
        return
    try:
        observation_service.record_observation(source=source, sku=sp.real_product.sku, value=value, raw=raw)
    except Exception:  # noqa: BLE001 — observation logging must never break the import pipeline
        logger.warning("record_observation failed for sp_id=%s", sp.id, exc_info=True)


def _detect_physical_change(raw_attributes: dict[str, Any], existing_data: dict[str, Any]) -> bool:
    for key in _PHYSICAL_KEYS:
        if key in raw_attributes and raw_attributes.get(key) != existing_data.get(key):
            return True
    return False


def _batched(iterator: Iterator, size: int) -> Iterator[list]:
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


_MAX_RETRY_ATTEMPTS = 10


def _run_batch_with_retry(fn, connector: BaseConnector, *args: Any, **kwargs: Any) -> Any:
    """Retries a single failed batch per `connector.should_retry`.

    `fn` performs one batch's DB writes; wrapping the attempt in `transaction.atomic()`
    means a failed attempt's partial writes (SourceProduct rows, audit drafts,
    Observations) never leak into the retried attempt — only the batch itself is
    re-run, not the whole full/delta sync. Hard cap of `_MAX_RETRY_ATTEMPTS` regardless
    of what the connector reports, so a misbehaving `should_retry` can never spin the
    task forever.

    `fn` MUST NOT run externally visible side-effects (signals to other modules, QMS
    writes, emergency primary-switch evaluation) — a rolled-back attempt cannot undo
    them and a retried attempt would emit them twice. Collect them instead and emit
    after this returns (see `_DeltaSideEffects` / `_emit_delta_side_effects`).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            with transaction.atomic():
                return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — retry policy decides re-raise vs retry
            if attempt >= _MAX_RETRY_ATTEMPTS or not connector.should_retry(exc, attempt):
                raise
            logger.warning("batch retrying attempt=%s after: %s", attempt, exc)


def execute_feed(feed: SourceFeed, *, mode: str = "full", source: str = "scheduler", triggered_by=None) -> ImportLog:
    log = log_service.start_import_log(feed, mode=mode, triggered_by=triggered_by, source=source)
    try:
        connector = connector_registry.get_connector(feed.connector_kind)
        if connector.is_async:
            if mode == "delta":
                connector.dispatch_fetch_delta(feed, log.run_id)
            else:
                connector.dispatch_fetch(feed, log.run_id)
            return log  # finalized in the scraper callback
        ctx = {"feed": feed, "mode": mode, "run_id": log.run_id}
        connector.before_fetch(ctx)
        counts = (
            process_delta_sync(feed, connector, log) if mode == "delta" else process_full_sync(feed, connector, log)
        )
        connector.after_fetch(ctx)
        status = LogStatus.PARTIAL.value if counts.get("error_count", 0) > 0 else LogStatus.SUCCESS.value
        finalized = log_service.finalize_import_log(log.run_id, status=status, **counts)
        if status == LogStatus.SUCCESS.value:
            source_products_imported_signal.send(sender=execute_feed, feed=feed, import_log=finalized)
        return finalized
    except Exception as exc:
        logger.exception("execute_feed failed for feed=%s", feed.id)
        log_service.finalize_import_log(
            log.run_id, status=LogStatus.FAILED.value, error_summary=[traceback.format_exc()[:5000]]
        )
        event_service.record(
            event_type=EventType.FEED_FAILED.value,
            severity=EventSeverity.CRITICAL.value,
            source=feed.source,
            feed=feed,
            message=str(exc) or exc.__class__.__name__,
        )
        return ImportLog.objects.get(run_id=log.run_id)


def _build_or_update_sp(
    *,
    source: Source,
    feed: SourceFeed,
    raw: RawProduct,
    existing_by_external: dict[str, SourceProduct],
    existing_by_ean: dict[str, SourceProduct],
    started_at: datetime,
) -> tuple[SourceProduct | None, str, str | None]:
    """Returns (instance, change_kind, rename_from) where change_kind ∈ {'new','updated','unchanged'}.

    `rename_from` is the external_id the EAN match matched under, only when this row's external_id
    just changed (never set for 'new'/'unchanged') — the caller needs it to enqueue a lookup refresh
    for the pre-rename ref too, whose row this write orphans otherwise.
    """
    new_hash = _hash_attributes(raw.attributes)
    sp = existing_by_external.get(raw.external_id)
    rename_from: str | None = None
    if sp is None and raw.ean:
        candidate = existing_by_ean.get(raw.ean)
        if candidate is not None and candidate.external_id != raw.external_id:
            sp = candidate
            rename_from = candidate.external_id

    if sp is None:
        return (
            SourceProduct(
                source=source,
                feed=feed,
                external_id=raw.external_id,
                external_id_history=[],
                name=raw.name,
                **_kind_routed_fields(source.kind, raw),
                currency=raw.currency or "",
                stock=raw.stock,
                ean=raw.ean or "",
                url=raw.url or "",
                image_urls=list(raw.images),
                data=dict(raw.attributes),
                data_hash=new_hash,
                status=ProductStatus.NEW.value,
                last_synced_at=started_at,
                data_changed_at=started_at,
            ),
            "new",
            None,
        )

    physical_changed = _detect_physical_change(raw.attributes, sp.data)
    data_changed = sp.data_hash != new_hash

    if rename_from is not None:
        history = list(sp.external_id_history) + [rename_from]
        sp.external_id_history = history[-_HISTORY_CAP:]
        sp.external_id = raw.external_id

    routed = _kind_routed_fields(source.kind, raw)
    sp.name = raw.name
    sp.cost = routed["cost"]
    sp.observed_price = routed["observed_price"]
    sp.signals = routed["signals"]
    sp.currency = raw.currency or ""
    sp.stock = raw.stock
    sp.ean = raw.ean or ""
    sp.url = raw.url or ""
    sp.image_urls = list(raw.images)
    sp.data = dict(raw.attributes)
    sp.last_synced_at = started_at

    if data_changed:
        sp.data_hash = new_hash
        sp.data_changed_at = started_at
    if physical_changed:
        sp.physical_changed_at = started_at

    if rename_from is not None or data_changed or physical_changed:
        return sp, "updated", rename_from
    return sp, "unchanged", None


_FULL_SYNC_UPDATE_FIELDS = [
    "external_id",
    "external_id_history",
    "feed",
    "name",
    "cost",
    "observed_price",
    "signals",
    "currency",
    "stock",
    "ean",
    "url",
    "image_urls",
    "data",
    "data_hash",
    "last_synced_at",
    "data_changed_at",
    "physical_changed_at",
]


def _process_full_sync_batch(
    source: Source, feed: SourceFeed, batch: list[RawProduct], started_at: datetime, observation_kind: bool
) -> dict[str, Any]:
    """Persist one fetch batch. The unit `_run_batch_with_retry` retries and wraps in
    `transaction.atomic()` — a failed attempt's writes never leak into the retry."""
    counts: dict[str, Any] = {
        "total_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "error_count": 0,
    }
    error_summary: list[str] = []

    external_ids = [r.external_id for r in batch]
    eans = [r.ean for r in batch if r.ean]
    base_qs = SourceProduct.objects.filter(source=source, external_id__in=external_ids)
    if observation_kind:
        base_qs = base_qs.select_related("real_product")
    existing_by_external = {sp.external_id: sp for sp in base_qs}
    existing_by_ean: dict[str, SourceProduct] = {}
    if eans:
        ean_qs = SourceProduct.objects.filter(source=source, ean__in=eans)
        if observation_kind:
            ean_qs = ean_qs.select_related("real_product")
        for sp in ean_qs:
            if sp.ean and sp.external_id not in existing_by_external:
                existing_by_ean[sp.ean] = sp

    to_create: list[SourceProduct] = []
    to_update: list[SourceProduct] = []
    audit_drafts: list[dict[str, Any]] = []
    pending_observations: list[tuple[SourceProduct, dict[str, Any]]] = []
    lookup_refs: list[str] = []

    for raw in batch:
        prev_sp = existing_by_external.get(raw.external_id)
        if prev_sp is None and raw.ean:
            prev_sp = existing_by_ean.get(raw.ean)
        prev_snapshot = _snapshot_for_audit(prev_sp)
        try:
            sp, kind, rename_from = _build_or_update_sp(
                source=source,
                feed=feed,
                raw=raw,
                existing_by_external=existing_by_external,
                existing_by_ean=existing_by_ean,
                started_at=started_at,
            )
        except Exception as exc:  # noqa: BLE001
            counts["error_count"] += 1
            if len(error_summary) < 50:
                error_summary.append(f"{raw.external_id}: {exc}")
            continue
        counts["total_count"] += 1
        if kind == "new":
            to_create.append(sp)
            counts["new_count"] += 1
            lookup_refs.append(_lookup_ref(source, sp.external_id))
        elif kind == "updated":
            to_update.append(sp)
            counts["updated_count"] += 1
            lookup_refs.append(_lookup_ref(source, sp.external_id))
            if rename_from is not None:
                # The pre-rename ref's own row is now orphaned — nothing else will ever refresh it
                # again under its old external_id, and a refresh of a ref the provider no longer
                # serves under that ref is exactly how django-lookup deletes the stale row.
                lookup_refs.append(_lookup_ref(source, rename_from))
        else:
            # unchanged still updates last_synced_at
            to_update.append(sp)
            counts["unchanged_count"] += 1
        # Audit only post-push mutations (operator interest); pre-push staging churn is noise.
        if prev_snapshot is not None and kind == "updated" and sp.status in PUSHED_STATUSES:
            audit_drafts.extend(_build_full_sync_audit_drafts(prev_snapshot, sp))
        if observation_kind and sp.real_product_id:
            pending_observations.append((sp, dict(raw.attributes)))

    if to_create:
        SourceProduct.objects.bulk_create(to_create, batch_size=_BATCH_SIZE)
    if to_update:
        SourceProduct.objects.bulk_update(to_update, fields=_FULL_SYNC_UPDATE_FIELDS, batch_size=_BATCH_SIZE)
    _flush_audit_drafts(audit_drafts, context="full_sync")
    # Written after the bulk persist succeeds — the observation log must never
    # race ahead of the staging table's actual state.
    for sp, raw_attrs in pending_observations:
        _record_observation_for_sp(source, sp, raw=raw_attrs)

    counts["error_summary"] = error_summary
    # NOT enqueued here — this whole function runs inside `_run_batch_with_retry`'s
    # `transaction.atomic()`. A Celery publish is not rolled back with a failed attempt and
    # would double-fire on a retry, so the refs travel out with `counts` and the caller
    # enqueues them only once `_run_batch_with_retry` has returned (see `_process_delta_sync_batch`
    # / `_emit_delta_side_effects` for the same rule already enforced on the delta-sync path).
    counts["lookup_refs"] = lookup_refs
    return counts


def process_full_sync(feed: SourceFeed, connector: BaseConnector, log: ImportLog) -> dict[str, int]:
    started_at = log.started_at
    source = feed.source

    counts: dict[str, Any] = {
        "total_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "delisted_count": 0,
        "error_count": 0,
        # split pushed-product delistings from non-pushed (`delisted_count` stays for backwards compat).
        "pushed_delisted_count": 0,
        "mass_delisting_triggered": False,
    }
    error_summary: list[str] = []

    raw_iter = iter(connector.fetch(feed))
    # Observation writes only apply to monitoring/enrichment kinds (procurement cost flows
    # via cost_updated_signal instead) — select_related("real_product") only when needed.
    observation_kind = source.kind in {SourceKind.MONITORING.value, SourceKind.ENRICHMENT.value}
    batch_size = connector.batch_size() or _BATCH_SIZE
    rate_limit_delay = connector.rate_limit_delay()

    for batch in _batched(raw_iter, batch_size):
        batch_counts = _run_batch_with_retry(
            _process_full_sync_batch, connector, source, feed, batch, started_at, observation_kind
        )
        for key in ("total_count", "new_count", "updated_count", "unchanged_count", "error_count"):
            counts[key] += batch_counts[key]
        error_summary.extend(batch_counts["error_summary"])
        # Only after the batch's transaction committed — exactly once per successful batch
        # (same rule `_emit_delta_side_effects` enforces on the delta-sync path).
        _enqueue_lookup_refresh(batch_counts["lookup_refs"])
        if rate_limit_delay:
            time.sleep(rate_limit_delay)

    # Delisting pass
    stale_qs = SourceProduct.objects.filter(source=source).exclude(last_synced_at__gte=started_at)
    pushed_total = SourceProduct.objects.filter(source=source, status__in=PUSHED_STATUSES).count()

    # Non-pushed stale rows: uniform status transition, no per-row side effect needed
    # -> single bulk UPDATE instead of an O(N) per-row .save() loop (mass-delisting path).
    stale_non_pushed = stale_qs.filter(status__in=_NON_PUSHED_DELIST_TARGETS)
    # Collected BEFORE the bulk write: a bulk UPDATE fires no post_save either, so without this a
    # delisted row keeps its fingerprint and stays proposable for a product that vanished from the
    # feed. REJECTED is excluded from lookup_provider.candidates(), so refreshing these refs after
    # the status flip is how django-lookup deletes the now-stale rows.
    delisted_refs = [
        _lookup_ref(source, external_id) for external_id in stale_non_pushed.values_list("external_id", flat=True)
    ]
    counts["delisted_count"] = stale_non_pushed.update(status=ProductStatus.REJECTED.value, modified_at=timezone.now())
    _enqueue_lookup_refresh(delisted_refs)

    # Pushed stale rows need an IntegrationEvent per row -- iterate only this (much smaller) subset.
    pushed_delisted = 0
    for sp in stale_qs.filter(status__in=PUSHED_STATUSES):
        pushed_delisted += 1
        counts["pushed_delisted_count"] += 1
        event_service.record(
            event_type=EventType.PUSHED_PRODUCT_DELISTED.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            feed=feed,
            message=f"Pushed SourceProduct {sp.external_id} disappeared from feed",
            details={"external_id": sp.external_id, "source_product_id": sp.id},
        )

    if pushed_total and pushed_delisted * 2 > pushed_total:  # >50%
        counts["mass_delisting_triggered"] = True
        event_service.record(
            event_type=EventType.MASS_DELISTING.value,
            severity=EventSeverity.WARNING.value,
            source=source,
            feed=feed,
            message=f"Mass delisting detected: {pushed_delisted}/{pushed_total} pushed products gone",
            details={"pushed_total": pushed_total, "pushed_delisted": pushed_delisted},
        )

    if error_summary:
        log.error_summary = error_summary[:50]
        log.save(update_fields=["error_summary", "modified_at"])
    return counts


_DELTA_SYNC_UPDATE_FIELDS = [
    "cost",
    "observed_price",
    "currency",
    "stock",
    "last_synced_at",
    "data_hash",
    "data_changed_at",
    "physical_changed_at",
]


@dataclass
class _DeltaSideEffects:
    """Externally visible per-row side-effects collected during a delta batch, emitted by
    `_emit_delta_side_effects` only AFTER the batch's `transaction.atomic()` committed.
    Firing them inside the batch meant a mid-batch rollback had already emitted signals /
    QMS writes for rows that never committed, and a retried batch emitted them twice."""

    cost_sps: list[SourceProduct] = dc_field(default_factory=list)
    stock_drops: list[tuple[SourceProduct, int | None, int | None]] = dc_field(default_factory=list)


def _emit_delta_side_effects(
    effects: _DeltaSideEffects, source: Source, *, settings: SourceSettings, warehouse: Any
) -> None:
    """Post-commit companion of `_process_delta_sync_batch` — QMS stock writes,
    pricemanager cost signals and emergency primary-switch evaluation, exactly once
    per successfully committed batch."""
    for sp in effects.cost_sps:
        _write_qms_cost_for_pushed_sp(sp, source, settings=settings, warehouse=warehouse)
    for sp, prev_stock, new_stock in effects.stock_drops:
        # emergency switch when primary just lost its stock. Best-effort;
        # never blocks delta sync. Real-time recovery beats waiting for cron.
        try:
            from django_atlas.services import primary_strategy_service

            primary_strategy_service.maybe_trigger_emergency_on_stock_drop(
                sp, previous_stock=prev_stock, new_stock=new_stock
            )
        except Exception:  # noqa: BLE001 — emergency eval must not break delta sync
            logger.warning("maybe_trigger_emergency_on_stock_drop failed for sp_id=%s", sp.id, exc_info=True)


def _process_delta_sync_batch(
    feed: SourceFeed,
    batch: list["PriceStockUpdate"],
    started_at: datetime,
    settings: SourceSettings,
    warehouse: Any,
    is_monitoring: bool,
    is_procurement: bool,
) -> tuple[dict[str, Any], _DeltaSideEffects]:
    """Persist one fetch-delta batch. The unit `_run_batch_with_retry` retries and wraps
    in `transaction.atomic()` — a failed attempt's writes never leak into the retry.
    Externally visible side-effects are NOT run here — they are collected into the
    returned `_DeltaSideEffects` and emitted by the caller after the batch commits."""
    source = feed.source
    counts: dict[str, Any] = {
        "total_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "error_count": 0,
        "physical_updated_count": 0,
        "physical_skipped_non_primary_count": 0,
        "physical_overwrite_count": 0,
    }

    external_ids = [u.external_id for u in batch]
    # select_related("real_product") avoids N+1 in _apply_physical_update + _write_qms_cost.
    existing = {
        sp.external_id: sp
        for sp in SourceProduct.objects.filter(source=source, external_id__in=external_ids).select_related(
            "real_product"
        )
    }
    # Pre-fetch primary links for every SKU in this batch — avoids a per-row
    # SourceProductLink query in _apply_physical_update_to_real_product.
    skus_in_batch = {sp.real_product.sku for sp in existing.values() if sp.real_product_id}
    links_by_sku = {
        link.real_product_sku: link
        for link in SourceProductLink.objects.filter(
            source_id=source.id, real_product_sku__in=skus_in_batch, is_active=True
        )
    }
    to_update: list[SourceProduct] = []
    audit_drafts: list[dict[str, Any]] = []
    pending_observations: list[SourceProduct] = []
    effects = _DeltaSideEffects()
    for upd in batch:
        counts["total_count"] += 1
        sp = existing.get(upd.external_id)
        if sp is None:
            counts["error_count"] += 1
            event_service.record(
                event_type=EventType.UNKNOWN_EXTERNAL_ID_IN_DELTA.value,
                severity=EventSeverity.INFO.value,
                source=source,
                feed=feed,
                message=f"Unknown external_id in delta feed: {upd.external_id}",
                details={"external_id": upd.external_id},
            )
            continue
        new_hash = _hash_attributes(
            {"cost": str(upd.cost) if upd.cost is not None else None, "currency": upd.currency, "stock": upd.stock}
        )
        # Kind routing: monitoring writes observed_price, procurement writes cost —
        # never both (SourceProduct.clean() rejects cross-kind writes; bulk_update
        # bypasses clean() so the routing must happen here). Enrichment writes neither
        # field, so `prev_price` must stay out of the `changed` calculation for it —
        # sp.cost is always None for enrichment SPs, and comparing against it would
        # spuriously flag every non-null upd.cost as a change.
        is_price_kind = is_monitoring or is_procurement
        prev_price = sp.observed_price if is_monitoring else sp.cost
        prev_cost, prev_currency, prev_stock = prev_price, sp.currency, sp.stock
        changed = (
            (is_price_kind and upd.cost is not None and upd.cost != prev_price)
            or (upd.currency is not None and upd.currency != sp.currency)
            or (upd.stock is not None and upd.stock != sp.stock)
        )
        if upd.cost is not None:
            if is_monitoring:
                sp.observed_price = upd.cost
            elif is_procurement:
                sp.cost = upd.cost
            # enrichment: PriceStockUpdate carries no signals concept — no-op
        if upd.currency is not None:
            sp.currency = upd.currency
        if upd.stock is not None:
            sp.stock = upd.stock
        sp.last_synced_at = started_at
        if changed:
            sp.data_hash = new_hash
            sp.data_changed_at = started_at
            counts["updated_count"] += 1
        else:
            counts["unchanged_count"] += 1
        to_update.append(sp)
        # Audit only for pushed SPs — pre-push delta churn is noise to PIM operators.
        # applied_to_pim=False: D5 cost defer means delta NOT yet propagated to pricing.
        if changed and sp.status in PUSHED_STATUSES:
            common = {"source_product": sp, "source": ChangeLogSource.DELTA_SYNC.value, "applied_to_pim": False}
            if is_price_kind and upd.cost is not None and upd.cost != prev_cost:
                audit_drafts.append(
                    {**common, "field_path": "cost", "before": _json_safe(prev_cost), "after": _json_safe(upd.cost)}
                )
            if upd.currency is not None and upd.currency != prev_currency:
                audit_drafts.append(
                    {**common, "field_path": "currency", "before": prev_currency, "after": upd.currency}
                )
            if upd.stock is not None and upd.stock != prev_stock:
                audit_drafts.append({**common, "field_path": "stock", "before": prev_stock, "after": upd.stock})
        physical_outcome = _apply_physical_update_to_real_product(sp, upd, started_at, links_by_sku=links_by_sku)
        if physical_outcome == PHYSICAL_OUTCOME_APPLIED:
            counts["physical_updated_count"] += 1
        elif physical_outcome == PHYSICAL_OUTCOME_SKIPPED_NON_PRIMARY:
            counts["physical_skipped_non_primary_count"] += 1
        elif physical_outcome == PHYSICAL_OUTCOME_OVERWRITTEN:
            counts["physical_overwrite_count"] += 1
        # Deferred to post-commit (`_emit_delta_side_effects`) — QMS/pricemanager writes and
        # emergency eval must never fire for a rolled-back attempt nor twice on retry.
        if sp.status in PUSHED_STATUSES:
            effects.cost_sps.append(sp)
        if changed and upd.stock is not None and upd.stock != prev_stock:
            effects.stock_drops.append((sp, prev_stock, upd.stock))
        if is_monitoring and sp.real_product_id:
            pending_observations.append(sp)

    if to_update:
        SourceProduct.objects.bulk_update(to_update, fields=_DELTA_SYNC_UPDATE_FIELDS, batch_size=_BATCH_SIZE)
    _flush_audit_drafts(audit_drafts, context="delta_sync")
    # Written after the bulk persist succeeds (same ordering rule as full sync) —
    # the observation log must never race ahead of the staging table's actual state.
    for sp in pending_observations:
        _record_observation_for_sp(source, sp)
    return counts, effects


def process_delta_sync(feed: SourceFeed, connector: BaseConnector, log: ImportLog) -> dict[str, int]:
    started_at = log.started_at
    source = feed.source

    counts: dict[str, Any] = {
        "total_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "delisted_count": 0,
        "error_count": 0,
        # delta sync never delists (no list of all SKUs); always zero/False, kept for shape parity.
        "pushed_delisted_count": 0,
        "mass_delisting_triggered": False,
        # physical updates were silently bucketed into updated_count (cost/qty only).
        # Split into 3 dedicated counters so operators can dashboard race-skip vs applied vs overwrite.
        "physical_updated_count": 0,
        "physical_skipped_non_primary_count": 0,
        "physical_overwrite_count": 0,
    }

    update_iter = iter(connector.fetch_delta(feed))

    # `source` is fixed for the whole call — resolve delta-sync-wide singletons ONCE instead
    # of once per row (perf: was up to ~1500 redundant queries/batch across the two writers).
    from django_atlas.services import qms_writer

    settings = SourceSettings.load()
    warehouse = qms_writer.resolve_warehouse(source)
    is_monitoring = source.kind == SourceKind.MONITORING.value
    is_procurement = source.kind == SourceKind.PROCUREMENT.value
    batch_size = connector.batch_size() or _BATCH_SIZE
    rate_limit_delay = connector.rate_limit_delay()

    for batch in _batched(update_iter, batch_size):
        batch_counts, side_effects = _run_batch_with_retry(
            _process_delta_sync_batch,
            connector,
            feed,
            batch,
            started_at,
            settings,
            warehouse,
            is_monitoring,
            is_procurement,
        )
        for key in (
            "total_count",
            "updated_count",
            "unchanged_count",
            "error_count",
            "physical_updated_count",
            "physical_skipped_non_primary_count",
            "physical_overwrite_count",
        ):
            counts[key] += batch_counts[key]
        # Only after the batch's transaction committed — exactly once per successful batch.
        _emit_delta_side_effects(side_effects, source, settings=settings, warehouse=warehouse)
        if rate_limit_delay:
            time.sleep(rate_limit_delay)
    return counts


_PHYSICAL_FIELDS = ("weight", "ean", "width", "height", "deep")
_PHYSICAL_DECIMAL_FIELDS = frozenset({"weight", "width", "height", "deep"})


def _write_qms_cost_for_pushed_sp(
    sp: SourceProduct, source: Source, *, settings: SourceSettings | None = None, warehouse: Any = None
) -> None:
    """Stage 5: delta sync of pushed SP triggers QMS stock + pricemanager cost log.

    `settings`/`warehouse` are resolved once per `process_delta_sync` call and passed in
    (not per row — both are invariant across the whole batch since `source` is fixed for
    the call). When omitted, the writers resolve them internally (single-call callers).
    Lazy imports keep stage-4-only environments happy.
    """
    if sp.status not in PUSHED_STATUSES:
        return
    from django_atlas.services import kind_guard, pricemanager_writer, qms_writer

    # Monitoring sources: SP row + change log update on delta, never QMS/PM writes.
    if kind_guard.is_push_blocked(source):
        return

    channels = list(sp.pushed_to_channel_idxs or [])
    write_stock_kwargs = {} if warehouse is None else {"warehouse": warehouse}
    qms_writer.write_stock(sp, source, channels, context="delta", settings=settings, **write_stock_kwargs)
    pricemanager_writer.log_cost(sp, source, channels, context="delta", settings=settings)


def _apply_physical_update_to_real_product(
    sp: SourceProduct,
    upd: "PriceStockUpdate",
    started_at,
    *,
    links_by_sku: dict[str, "SourceProductLink"] | None = None,
) -> str:
    """Apply physical fields (weight/ean/dims) to RealProduct (shared across channels).

    Only for already-pushed SPs (status pushed / pushed_pending_images) — INIT/manual SPs
    have no real_product yet. Physical changes update RealProduct directly (single source
    of truth across channels) and stamp `physical_changed_at` on SP.

    primary-only writes. Non-primary sources skip by default;
    opt-in via Source.allow_physical_writes_from_non_primary restores legacy
    last-write-wins with a warning audit. Returns one of the PHYSICAL_OUTCOME_* constants
    so the caller can bucket counts without re-querying.
    """
    from decimal import Decimal, InvalidOperation

    from django_atlas.services import kind_guard

    if upd.physical is None:
        return PHYSICAL_OUTCOME_NOOP
    if sp.status not in (ProductStatus.PUSHED.value, ProductStatus.PUSHED_PENDING_IMAGES.value):
        return PHYSICAL_OUTCOME_NOOP
    # Monitoring sources never write RealProduct physical fields.
    if kind_guard.is_push_blocked(sp.source):
        return PHYSICAL_OUTCOME_NOOP
    if sp.real_product_id is None:
        return PHYSICAL_OUTCOME_NOOP

    # primary-only physical writes gate. `links_by_sku` (pre-fetched once per
    # batch by the caller) avoids a per-row query; falls back to a direct lookup otherwise.
    if links_by_sku is not None:
        link = links_by_sku.get(sp.real_product.sku)
    else:
        link = SourceProductLink.objects.filter(
            real_product_sku=sp.real_product.sku, source_id=sp.source_id, is_active=True
        ).first()
    overwrite_mode = False
    if link is None or not link.is_primary:
        if not sp.source.allow_physical_writes_from_non_primary:
            _emit_physical_race_skip(sp, upd)
            return PHYSICAL_OUTCOME_SKIPPED_NON_PRIMARY
        overwrite_mode = True

    rp = sp.real_product
    changed_fields: list[str] = []
    physical_diffs: list[tuple[str, Any, Any]] = []

    for field in _PHYSICAL_FIELDS:
        new_value = upd.physical.get(field)
        if new_value is None:
            continue
        if field in _PHYSICAL_DECIMAL_FIELDS:
            try:
                new_value = Decimal(str(new_value))
            except (InvalidOperation, ValueError, TypeError):
                continue
        current = getattr(rp, field)
        if current == new_value:
            continue
        physical_diffs.append((field, current, new_value))
        setattr(rp, field, new_value)
        changed_fields.append(field)

    if not changed_fields:
        # Race gate passed but every field matched current state — treat as noop for counts.
        return PHYSICAL_OUTCOME_NOOP

    if "ean" in changed_fields:
        rp.save(update_fields=changed_fields, ignore_validate_ean=True)
    else:
        rp.save(update_fields=changed_fields)
    sp.physical_changed_at = started_at

    if overwrite_mode:
        # Opt-in path: legacy last-write-wins, but loudly audited so the operator never loses sight.
        primary_idx = (
            SourceProductLink.objects.filter(real_product_sku=rp.sku, is_active=True, is_primary=True)
            .values_list("source__idx", flat=True)
            .first()
        )
        event_service.record(
            event_type=EventType.PHYSICAL_UPDATE_OVERWRITE.value,
            severity=EventSeverity.WARNING.value,
            source=sp.source,
            source_product=sp,
            message=(
                f"Non-primary source {sp.source.idx} overwrote RealProduct physical "
                f"fields for sku={rp.sku}: {changed_fields} (opt-in)"
            ),
            details={
                "sku": rp.sku,
                "changed_fields": changed_fields,
                "non_primary_source_idx": sp.source.idx,
                "primary_source_idx": primary_idx,
            },
        )
        audit_source = ChangeLogSource.PHYSICAL_OVERWRITE.value
        outcome = PHYSICAL_OUTCOME_OVERWRITTEN
    else:
        event_service.record(
            event_type=EventType.PHYSICAL_UPDATE_APPLIED.value,
            severity=EventSeverity.INFO.value,
            source=sp.source,
            source_product=sp,
            message=f"RealProduct physical updated for sku={rp.sku}: {changed_fields}",
            details={"changed_fields": changed_fields, "sku": rp.sku},
        )
        audit_source = ChangeLogSource.DELTA_SYNC.value
        outcome = PHYSICAL_OUTCOME_APPLIED

    # Audit: applied_to_pim=True — RealProduct is the single source of truth across channels.
    drafts = [
        {
            "source_product": sp,
            "real_product_sku": rp.sku,
            "source": audit_source,
            "field_path": f"physical.{field}",
            "before": _json_safe(prev),
            "after": _json_safe(new),
            "applied_to_pim": True,
        }
        for field, prev, new in physical_diffs
    ]
    _flush_audit_drafts(drafts, context="physical_update")
    return outcome


def _emit_physical_race_skip(sp: SourceProduct, upd: "PriceStockUpdate") -> None:
    """non-primary source tried to write physical fields — drop, audit, event.

    Best-effort: event + audit failures are logged but never re-raised (matches the
    posture of `_flush_audit_drafts`). The data path is the source of truth; observability
    is opportunistic.
    """
    from django_atlas.models import SourceProductLink

    rp = sp.real_product
    attempted = sorted(k for k, v in (upd.physical or {}).items() if v is not None)
    primary_idx = (
        SourceProductLink.objects.filter(real_product_sku=rp.sku, is_active=True, is_primary=True)
        .values_list("source__idx", flat=True)
        .first()
    )
    try:
        event_service.record(
            event_type=EventType.PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY.value,
            severity=EventSeverity.INFO.value,
            source=sp.source,
            source_product=sp,
            message=(f"Skipped physical update from non-primary source {sp.source.idx} on sku={rp.sku}"),
            details={
                "sku": rp.sku,
                "source_idx": sp.source.idx,
                "primary_source_idx": primary_idx,
                "attempted_fields": attempted,
            },
        )
    except Exception:  # noqa: BLE001 — observability must never block delta sync
        logger.warning("event_service.record failed for physical race skip sp=%s", sp.id, exc_info=True)

    drafts = [
        {
            "source_product": sp,
            "real_product_sku": rp.sku,
            "source": ChangeLogSource.PHYSICAL_SKIPPED.value,
            "field_path": "physical_skipped",
            "before": None,
            "after": _json_safe({k: v for k, v in (upd.physical or {}).items() if v is not None}),
            "applied_to_pim": False,
        }
    ]
    _flush_audit_drafts(drafts, context="physical_race_skip")


def process_feed_results(run_id: UUID, raw_products: list[RawProduct]) -> ImportLog:
    """Async callback: take the worker-delivered raw_products, persist, finalize."""
    log = ImportLog.objects.select_related("feed", "feed__source").get(run_id=run_id)
    feed = log.feed
    source = feed.source

    started_at = log.started_at
    counts: dict[str, Any] = {
        "total_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "delisted_count": 0,
        "error_count": 0,
        # scraper-callback path never runs delisting pass; kept zero for shape parity.
        "pushed_delisted_count": 0,
        "mass_delisting_triggered": False,
    }

    if raw_products:
        external_ids = [r.external_id for r in raw_products]
        eans = [r.ean for r in raw_products if r.ean]
        existing_by_external = {
            sp.external_id: sp for sp in SourceProduct.objects.filter(source=source, external_id__in=external_ids)
        }
        existing_by_ean: dict[str, SourceProduct] = {}
        if eans:
            for sp in SourceProduct.objects.filter(source=source, ean__in=eans):
                if sp.ean and sp.external_id not in existing_by_external:
                    existing_by_ean[sp.ean] = sp

        to_create: list[SourceProduct] = []
        to_update: list[SourceProduct] = []
        for raw in raw_products:
            sp, kind, _rename_from = _build_or_update_sp(
                source=source,
                feed=feed,
                raw=raw,
                existing_by_external=existing_by_external,
                existing_by_ean=existing_by_ean,
                started_at=started_at,
            )
            counts["total_count"] += 1
            if kind == "new":
                to_create.append(sp)
                counts["new_count"] += 1
            elif kind == "updated":
                to_update.append(sp)
                counts["updated_count"] += 1
            else:
                to_update.append(sp)
                counts["unchanged_count"] += 1

        if to_create:
            SourceProduct.objects.bulk_create(to_create, batch_size=_BATCH_SIZE)
        if to_update:
            SourceProduct.objects.bulk_update(
                to_update,
                fields=[
                    "external_id",
                    "external_id_history",
                    "feed",
                    "name",
                    "cost",
                    "currency",
                    "stock",
                    "ean",
                    "url",
                    "image_urls",
                    "data",
                    "data_hash",
                    "last_synced_at",
                    "data_changed_at",
                    "physical_changed_at",
                ],
                batch_size=_BATCH_SIZE,
            )

    status = LogStatus.PARTIAL.value if counts["error_count"] > 0 else LogStatus.SUCCESS.value
    finalized = log_service.finalize_import_log(log.run_id, status=status, **counts)
    if status == LogStatus.SUCCESS.value:
        source_products_imported_signal.send(sender=process_feed_results, feed=feed, import_log=finalized)
    return finalized


def update_or_create_source_product(
    source: Source, feed: SourceFeed, raw_product: RawProduct
) -> tuple[SourceProduct, bool]:
    """Helper for unit tests / single-item import. Returns (instance, created)."""
    started_at = timezone.now()
    existing_by_external = {}
    existing_by_ean = {}
    sp = SourceProduct.objects.filter(source=source, external_id=raw_product.external_id).first()
    if sp is not None:
        existing_by_external[sp.external_id] = sp
    elif raw_product.ean:
        ean_match = SourceProduct.objects.filter(source=source, ean=raw_product.ean).first()
        if ean_match is not None:
            existing_by_ean[ean_match.ean] = ean_match
    instance, kind, _rename_from = _build_or_update_sp(
        source=source,
        feed=feed,
        raw=raw_product,
        existing_by_external=existing_by_external,
        existing_by_ean=existing_by_ean,
        started_at=started_at,
    )
    if kind == "new":
        instance.save()
        return instance, True
    instance.save()
    return instance, False


__all__ = [
    "execute_feed",
    "process_delta_sync",
    "process_feed_results",
    "process_full_sync",
    "update_or_create_source_product",
]


# Avoid F401 for PriceStockUpdate (re-export for typing convenience downstream)
_ = PriceStockUpdate
