# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Auto-primary selection service.

`evaluate_primary_source(rp)` returns a `PrimaryEvalResult` describing the
auto-picker decision for a single PIM RealProduct (multi-source scenario only —
single-link RPs always resolve to `NO_CHANGE`). `apply_primary_switch` performs
the atomic flip (unset old, set new, audit, signal). Operator-facing entry points
are `force_set_primary` (manual override sticky) and `reset_to_auto`.

Decision logic — Strategy: `lowest_cost_with_stock` (default). Candidates are
active links where the latest SourceProduct has `stock > 0` and a non-null
`cost`. Ordered by cost ASC, source_id ASC (deterministic on ties).

Safety gates (skippable via `bypass_safety=True` for emergency stock=0 flow):
  - manual_override sticky — any link on RP has `manual_override=True` → skip
  - cooldown — `now - current.preferred_changed_at < cooldown_hours` → skip
  - hysteresis — cost improvement % < `Source.primary_switch_hysteresis_pct` → skip
  - no_change — winner equals current primary → skip (idempotent, no audit spam)

Atomicity — `apply_primary_switch` wraps the unset-old + set-new in
`transaction.atomic()` so concurrent cron + inline trigger cannot leave the RP
with two primary links. `primary_switched_signal` dispatch wraps in
`transaction.on_commit` so subscribers fire only after a successful commit.

Audit trail — every applied switch writes one `SourceProductChangeLog` row
on the NEW primary SP (`field_path=is_primary`). Skips emit only the
matching `IntegrationEvent` (info severity) — no audit row, since no state
change happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType, PrimarySkipReason, PrimaryStrategy
from django_atlas.models import SourceProduct, SourceProductLink
from django_atlas.services import audit_service, event_service
from django_atlas.signals import primary_switched_signal

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)


# Service-level default applied when a source somehow has hysteresis_pct=0
# (operator opt-out) but we still want to gate against no-op flips. Keeps the
# evaluator deterministic when the per-source setting is intentionally lax.
_MIN_HYSTERESIS_FLOOR_PCT = Decimal("0.0")


@dataclass
class PrimaryCandidate:
    """A link + its latest SourceProduct cost/stock summary used by the picker."""

    link: SourceProductLink
    source_product: SourceProduct
    cost: Decimal
    stock: int


@dataclass
class PrimaryEvalResult:
    """Outcome of `evaluate_primary_source` — never raises, always returns a result.

    `decision_audit` collects everything needed for the operator playbook: which
    strategy ran, how many candidates were considered, current cost vs. winner
    cost, cooldown remaining, etc. Stored in IntegrationEvent.details so a
    failed/skipped evaluation is debuggable from the CMS Events panel alone.
    """

    real_product_sku: str
    current_primary: SourceProductLink | None
    new_primary: SourceProductLink | None
    should_switch: bool
    skip_reason: PrimarySkipReason
    decision_audit: dict[str, Any] = field(default_factory=dict)


def _summarise_link(link: SourceProductLink) -> PrimaryCandidate | None:
    """Pull the latest SourceProduct cost+stock for a link. None when unusable."""
    from django_atlas.services import kind_guard

    # Monitoring sources never become primary — primary drives physical/cost writes.
    if kind_guard.is_push_blocked(link.source):
        return None
    sp = (
        SourceProduct.objects.filter(source=link.source, external_id=link.external_id)
        .order_by("-modified_at", "-id")
        .first()
    )
    if sp is None or sp.cost is None or sp.stock is None:
        return None
    return PrimaryCandidate(link=link, source_product=sp, cost=Decimal(sp.cost), stock=int(sp.stock))


def _cost_improvement_pct(current: Decimal, candidate: Decimal) -> Decimal:
    """Return the percent the candidate is cheaper than the current primary.

    Positive means cheaper. Negative means more expensive. Floor 0 when current
    is 0 to avoid divide-by-zero (zero-cost primary = anything else wins
    automatically because operators only set zero cost as a placeholder).
    """
    if current <= 0:
        return Decimal("100.0") if candidate < current else Decimal("0.0")
    return ((current - candidate) / current * Decimal("100")).quantize(Decimal("0.0001"))


def _emit_skip_event(
    *,
    rp_sku: str,
    skip_reason: PrimarySkipReason,
    current: SourceProductLink | None,
    candidate: SourceProductLink | None,
    decision_audit: dict[str, Any],
    event_sink: list[dict[str, Any]] | None,
) -> None:
    """Map skip reason → IntegrationEvent type + severity. Best-effort, swallows errors."""
    mapping = {
        PrimarySkipReason.COOLDOWN: (EventType.PRIMARY_SWITCH_SKIPPED_COOLDOWN, EventSeverity.INFO),
        PrimarySkipReason.HYSTERESIS: (EventType.PRIMARY_SWITCH_SKIPPED_HYSTERESIS, EventSeverity.INFO),
        PrimarySkipReason.MANUAL_OVERRIDE: (EventType.PRIMARY_SWITCH_SKIPPED_MANUAL_OVERRIDE, EventSeverity.INFO),
    }
    pair = mapping.get(skip_reason)
    if pair is None:
        return
    event_type, severity = pair
    details = {
        "real_product_sku": rp_sku,
        "current_primary_source_idx": current.source.idx if current else None,
        "candidate_source_idx": candidate.source.idx if candidate else None,
        **decision_audit,
    }
    message = f"Primary switch skipped on '{rp_sku}': {skip_reason.value}"
    try:
        event_service.record(
            event_type=event_type.value,
            severity=severity.value,
            message=message,
            source=current.source if current else None,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log is best-effort
        logger.warning("event_service.record failed for %s", event_type.value, exc_info=True)
    if event_sink is not None:
        event_sink.append(
            {"event_type": event_type.value, "severity": severity.value, "message": message, "details": details}
        )


def evaluate_primary_source(
    real_product_sku: str, *, bypass_safety: bool = False, event_sink: list[dict[str, Any]] | None = None
) -> PrimaryEvalResult:
    """Decide which SourceProductLink should be primary for a RealProduct.

    `bypass_safety=True` skips cooldown + hysteresis (used by emergency stock=0
    trigger). manual_override sticky bit is ALWAYS respected, even in emergency
    mode — operator intent wins.
    """
    links = list(
        SourceProductLink.objects.filter(real_product_sku=real_product_sku, is_active=True).select_related("source")
    )
    current_primary = next((link for link in links if link.is_primary), None)

    decision_audit: dict[str, Any] = {
        "strategy": PrimaryStrategy.LOWEST_COST_WITH_STOCK.value,
        "candidates_count": 0,
        "links_count": len(links),
        "bypass_safety": bypass_safety,
    }

    if not links:
        return PrimaryEvalResult(
            real_product_sku=real_product_sku,
            current_primary=None,
            new_primary=None,
            should_switch=False,
            skip_reason=PrimarySkipReason.NO_CANDIDATES,
            decision_audit=decision_audit,
        )

    # Manual override is sticky — always respected. Single source of truth.
    if any(link.manual_override for link in links):
        _emit_skip_event(
            rp_sku=real_product_sku,
            skip_reason=PrimarySkipReason.MANUAL_OVERRIDE,
            current=current_primary,
            candidate=None,
            decision_audit=decision_audit,
            event_sink=event_sink,
        )
        return PrimaryEvalResult(
            real_product_sku=real_product_sku,
            current_primary=current_primary,
            new_primary=None,
            should_switch=False,
            skip_reason=PrimarySkipReason.MANUAL_OVERRIDE,
            decision_audit=decision_audit,
        )

    candidates = [c for c in (_summarise_link(link) for link in links) if c is not None and c.stock > 0]
    decision_audit["candidates_count"] = len(candidates)

    if not candidates:
        return PrimaryEvalResult(
            real_product_sku=real_product_sku,
            current_primary=current_primary,
            new_primary=None,
            should_switch=False,
            skip_reason=PrimarySkipReason.NO_CANDIDATES,
            decision_audit=decision_audit,
        )

    # lowest_cost_with_stock — ASC by cost, then by source_id for ties.
    candidates.sort(key=lambda c: (c.cost, c.link.source_id))
    winner = candidates[0]
    decision_audit["winner_source_idx"] = winner.link.source.idx
    decision_audit["winner_cost"] = str(winner.cost)
    decision_audit["winner_stock"] = winner.stock

    if current_primary is not None and current_primary.pk == winner.link.pk:
        decision_audit["reason_detail"] = "Winner equals current primary."
        return PrimaryEvalResult(
            real_product_sku=real_product_sku,
            current_primary=current_primary,
            new_primary=winner.link,
            should_switch=False,
            skip_reason=PrimarySkipReason.NO_CHANGE,
            decision_audit=decision_audit,
        )

    if current_primary is not None:
        current_summary = _summarise_link(current_primary)
        if current_summary is not None:
            decision_audit["current_cost"] = str(current_summary.cost)
            decision_audit["current_stock"] = current_summary.stock
            improvement_pct = _cost_improvement_pct(current_summary.cost, winner.cost)
            decision_audit["cost_improvement_pct"] = str(improvement_pct)
            if not bypass_safety:
                hysteresis = max(
                    Decimal(current_primary.source.primary_switch_hysteresis_pct), _MIN_HYSTERESIS_FLOOR_PCT
                )
                decision_audit["hysteresis_pct"] = str(hysteresis)
                if improvement_pct < hysteresis:
                    _emit_skip_event(
                        rp_sku=real_product_sku,
                        skip_reason=PrimarySkipReason.HYSTERESIS,
                        current=current_primary,
                        candidate=winner.link,
                        decision_audit=decision_audit,
                        event_sink=event_sink,
                    )
                    return PrimaryEvalResult(
                        real_product_sku=real_product_sku,
                        current_primary=current_primary,
                        new_primary=winner.link,
                        should_switch=False,
                        skip_reason=PrimarySkipReason.HYSTERESIS,
                        decision_audit=decision_audit,
                    )
        # Cooldown — respects current primary's source setting.
        if not bypass_safety and current_primary.preferred_changed_at is not None:
            cooldown_hours = current_primary.source.primary_switch_cooldown_hours
            elapsed = timezone.now() - current_primary.preferred_changed_at
            cooldown_remaining = timedelta(hours=cooldown_hours) - elapsed
            decision_audit["cooldown_hours"] = cooldown_hours
            decision_audit["cooldown_remaining_h"] = max(0.0, cooldown_remaining.total_seconds() / 3600.0)
            if cooldown_remaining.total_seconds() > 0:
                _emit_skip_event(
                    rp_sku=real_product_sku,
                    skip_reason=PrimarySkipReason.COOLDOWN,
                    current=current_primary,
                    candidate=winner.link,
                    decision_audit=decision_audit,
                    event_sink=event_sink,
                )
                return PrimaryEvalResult(
                    real_product_sku=real_product_sku,
                    current_primary=current_primary,
                    new_primary=winner.link,
                    should_switch=False,
                    skip_reason=PrimarySkipReason.COOLDOWN,
                    decision_audit=decision_audit,
                )

    return PrimaryEvalResult(
        real_product_sku=real_product_sku,
        current_primary=current_primary,
        new_primary=winner.link,
        should_switch=True,
        skip_reason=PrimarySkipReason.NONE,
        decision_audit=decision_audit,
    )


def _resolve_audit_sp(link: SourceProductLink) -> SourceProduct | None:
    """The audit row lives on a SourceProduct (FK). Pick the latest SP for the link."""
    return (
        SourceProduct.objects.filter(source=link.source, external_id=link.external_id)
        .order_by("-modified_at", "-id")
        .first()
    )


def apply_primary_switch(
    *,
    new_link: SourceProductLink,
    previous_link: SourceProductLink | None,
    reason_source: ChangeLogSource,
    event_type: EventType,
    severity: EventSeverity,
    decision_audit: dict[str, Any],
    triggered_by: AbstractBaseUser | None = None,
    event_sink: list[dict[str, Any]] | None = None,
) -> None:
    """Atomic flip — unset previous, set new, audit, dispatch signal.

    Wrapped in `transaction.atomic()` so a partial failure rolls back both
    `is_primary` updates. Signal dispatch waits for commit.
    """
    rp_sku = new_link.real_product_sku
    now = timezone.now()
    with transaction.atomic():
        SourceProductLink.objects.filter(real_product_sku=rp_sku, is_primary=True).exclude(pk=new_link.pk).update(
            is_primary=False
        )
        new_link.is_primary = True
        new_link.preferred_changed_at = now
        new_link.save(update_fields=["is_primary", "preferred_changed_at", "modified_at"])

    sp = _resolve_audit_sp(new_link)
    if sp is not None:
        try:
            audit_service.log_change(
                source_product=sp,
                source=reason_source.value,
                field_path="source_product_link.is_primary",
                before={
                    "source_idx": previous_link.source.idx if previous_link else None,
                    "link_pk": previous_link.pk if previous_link else None,
                },
                after={"source_idx": new_link.source.idx, "link_pk": new_link.pk},
                triggered_by=triggered_by,
                applied_to_pim=True,
                applied_to_pim_at=now,
                real_product_sku=rp_sku,
            )
        except Exception:  # noqa: BLE001 — audit must be best-effort
            logger.warning("audit_service.log_change failed for %s", reason_source.value, exc_info=True)

    message = (
        f"Primary source for '{rp_sku}' switched "
        f"from '{previous_link.source.idx if previous_link else None}' to '{new_link.source.idx}'."
    )
    details = {
        "real_product_sku": rp_sku,
        "from_source_idx": previous_link.source.idx if previous_link else None,
        "to_source_idx": new_link.source.idx,
        "source": reason_source.value,
        **decision_audit,
    }
    try:
        event_service.record(
            event_type=event_type.value,
            severity=severity.value,
            message=message,
            source=new_link.source,
            source_product=sp,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event must be best-effort
        logger.warning("event_service.record failed for %s", event_type.value, exc_info=True)

    if event_sink is not None:
        event_sink.append(
            {"event_type": event_type.value, "severity": severity.value, "message": message, "details": details}
        )

    def _dispatch() -> None:
        try:
            primary_switched_signal.send(
                sender=apply_primary_switch,
                real_product_sku=rp_sku,
                from_source_idx=previous_link.source.idx if previous_link else None,
                to_source_idx=new_link.source.idx,
                reason=reason_source.value,
                source=reason_source.value,
            )
        except Exception:  # noqa: BLE001 — receivers must not break the switch
            logger.warning("primary_switched_signal dispatch failed for %s", rp_sku, exc_info=True)

    transaction.on_commit(_dispatch)


def evaluate_and_apply(
    real_product_sku: str,
    *,
    bypass_safety: bool = False,
    triggered_by: AbstractBaseUser | None = None,
    event_sink: list[dict[str, Any]] | None = None,
) -> PrimaryEvalResult:
    """Convenience: evaluate, then apply if should_switch=True.

    Picks the matching audit `source` + IntegrationEvent type for the trigger:
      bypass_safety=True → EMERGENCY_SWITCH / PRIMARY_SOURCE_EMERGENCY_SWITCH
      bypass_safety=False → AUTO_PRIMARY_SWITCH / PRIMARY_SOURCE_SWITCHED
    """
    result = evaluate_primary_source(real_product_sku, bypass_safety=bypass_safety, event_sink=event_sink)
    if not result.should_switch or result.new_primary is None:
        return result
    if bypass_safety:
        reason_source = ChangeLogSource.EMERGENCY_SWITCH
        event_type = EventType.PRIMARY_SOURCE_EMERGENCY_SWITCH
        severity = EventSeverity.WARNING
    else:
        reason_source = ChangeLogSource.AUTO_PRIMARY_SWITCH
        event_type = EventType.PRIMARY_SOURCE_SWITCHED
        severity = EventSeverity.INFO
    apply_primary_switch(
        new_link=result.new_primary,
        previous_link=result.current_primary,
        reason_source=reason_source,
        event_type=event_type,
        severity=severity,
        decision_audit=result.decision_audit,
        triggered_by=triggered_by,
        event_sink=event_sink,
    )
    return result


def force_set_primary(
    *,
    real_product_sku: str,
    source_idx: str,
    reason: str,
    triggered_by: AbstractBaseUser | None = None,
    event_sink: list[dict[str, Any]] | None = None,
) -> SourceProductLink:
    """Operator force-set primary + sticky `manual_override=True`.

    Cron and inline auto-eval skip the RP until reset-to-auto clears the flag.
    Emits `primary_source_forced` (warning) because forcing usually goes
    against auto-strategy — operator awareness is the whole point.

    Raises ValueError when source is not linked to the SKU.
    """
    if not reason or len(reason.strip()) < 3:
        raise ValueError("reason must be at least 3 characters")
    try:
        new_link = SourceProductLink.objects.select_related("source").get(
            real_product_sku=real_product_sku, source__idx=source_idx
        )
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(f"SourceProductLink for sku='{real_product_sku}' source='{source_idx}' not found") from exc

    from django_atlas.services import kind_guard

    kind_guard.assert_push_allowed(new_link.source, "force-set as primary source")

    previous_link = (
        SourceProductLink.objects.filter(real_product_sku=real_product_sku, is_primary=True)
        .exclude(pk=new_link.pk)
        .select_related("source")
        .first()
    )

    eval_snapshot = evaluate_primary_source(real_product_sku, event_sink=None)
    decision_audit = {
        **eval_snapshot.decision_audit,
        "reason": reason.strip(),
        "operator_id": getattr(triggered_by, "pk", None),
        "manual_override": True,
        "auto_would_pick": (eval_snapshot.new_primary.source.idx if eval_snapshot.new_primary else None),
    }

    apply_primary_switch(
        new_link=new_link,
        previous_link=previous_link,
        reason_source=ChangeLogSource.MANUAL_OVERRIDE,
        event_type=EventType.PRIMARY_SOURCE_FORCED,
        severity=EventSeverity.WARNING,
        decision_audit=decision_audit,
        triggered_by=triggered_by,
        event_sink=event_sink,
    )

    # Sticky bit MUST be set after the flip so cron skips this RP going forward.
    SourceProductLink.objects.filter(real_product_sku=real_product_sku).update(manual_override=False)
    new_link.manual_override = True
    new_link.save(update_fields=["manual_override", "modified_at"])
    return new_link


def reset_to_auto(
    *,
    real_product_sku: str,
    triggered_by: AbstractBaseUser | None = None,
    event_sink: list[dict[str, Any]] | None = None,
) -> PrimaryEvalResult:
    """Clear manual_override sticky bits on every link, then run auto-eval inline.

    The returned `PrimaryEvalResult` reflects what happened: either a switch
    fired (and was applied) or a skip reason explains why the current state
    remained. Cooldown is bypassed for the inline trigger so the operator gets
    immediate feedback after clicking 'Reset to auto'.
    """
    SourceProductLink.objects.filter(real_product_sku=real_product_sku).update(manual_override=False)
    # Cooldown bypass on reset is deliberate — operator just asked for auto-strategy NOW.
    return evaluate_and_apply(real_product_sku, bypass_safety=True, triggered_by=triggered_by, event_sink=event_sink)


def maybe_trigger_inline_after_link(
    real_product_sku: str, *, event_sink: list[dict[str, Any]] | None = None
) -> PrimaryEvalResult | None:
    """Called by `_write_qms_cost_link` after `upsert_for_push` creates a link.

    Bails when only one active link exists for the SKU (nothing to evaluate —
    `set_primary_if_first=True` already handled the single-link case).
    Otherwise runs `evaluate_and_apply` without bypass_safety so hysteresis +
    cooldown still apply on the natural multi-source path.
    """
    link_count = SourceProductLink.objects.filter(real_product_sku=real_product_sku, is_active=True).count()
    if link_count < 2:
        return None
    return evaluate_and_apply(real_product_sku, bypass_safety=False, event_sink=event_sink)


def maybe_trigger_emergency_on_stock_drop(
    source_product: SourceProduct,
    *,
    previous_stock: int | None,
    new_stock: int | None,
    event_sink: list[dict[str, Any]] | None = None,
) -> PrimaryEvalResult | None:
    """Called by delta sync after a stock update on a pushed SP.

    Triggers `evaluate_and_apply(bypass_safety=True)` ONLY when:
      - the SP belongs to the current primary SourceProductLink for its RP
      - stock just dropped to <= 0 from a positive value (avoid spam when it was
        already 0)

    Cooldown + hysteresis are bypassed so storefront recovers within one delta
    cycle. manual_override is still respected — operator intent wins even in
    emergency.
    """
    if source_product.real_product_id is None:
        return None
    if new_stock is None or new_stock > 0:
        return None
    if previous_stock is not None and previous_stock <= 0:
        return None
    rp_sku = source_product.real_product.sku
    try:
        link = SourceProductLink.objects.get(real_product_sku=rp_sku, source=source_product.source, is_primary=True)
    except SourceProductLink.DoesNotExist:
        return None
    if not link.is_active:
        return None
    return evaluate_and_apply(rp_sku, bypass_safety=True, event_sink=event_sink)


def iter_multi_source_real_product_skus(*, chunk_size: int = 200) -> list[str]:
    """Return distinct real_product_sku values where >1 active link exists.

    Used by the cron task to drive batch evaluation. `chunk_size` reserved for
    future streaming version — for now returns a materialised list because
    aggregation on `Count('id')` cannot iter() across Postgres cursors when the
    filter touches the same column.
    """
    qs = (
        SourceProductLink.objects.filter(is_active=True)
        .values("real_product_sku")
        .annotate(link_count=Count("id"))
        .filter(link_count__gt=1)
        .values_list("real_product_sku", flat=True)
    )
    return list(qs)
