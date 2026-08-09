# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Read-side API for SourceProductChangeLog.

Mirror counterpart to `audit_service` (which is write-only). Powers that stage's
4 admin endpoints: timeline (`list_for_sku`), bulk badge lookup
(`bulk_has_changes`), `acknowledge`, and `force_repush_by_sku`.

Index strategy: every read path is shaped to hit one of the 3 model indexes —
`atlas_changelog_sku_unseen_idx` carries `(real_product_sku, applied_to_pim, -created_at)`
which covers both timeline filters and the bulk aggregate.

`acknowledge` is the only mutation here and it ALSO emits an audit entry: the ack itself is traceable —
who/when/which change_ids. Out-of-band: emit is best-effort, ack mutation
never blocks on audit write.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Count, Max, Q
from django.utils import timezone

from django_atlas.enums import CHANGE_LOG_SOURCES, ChangeLogSource
from django_atlas.models import SourceProduct, SourceProductChangeLog, SourceProductLink
from django_atlas.services import audit_service, product_link_service, push_service

logger = logging.getLogger(__name__)

_MAX_TIMELINE = 100


# ---------------------------------------------------------------------------
# Single-SKU timeline
# ---------------------------------------------------------------------------


def list_for_sku(
    sku: str, *, since: datetime | None = None, sources: list[str] | None = None, unseen_only: bool = False
) -> dict[str, Any]:
    """Return the full payload for `GET /pim-sku/{sku}/changes/`.

    Existence policy:
      - If no `SourceProductLink` AND no audit rows for this SKU → ValueError
        ("PIM SKU ... not found") so the view emits 404. We do NOT touch the PIM
        RealProduct table here — the source module is the source of truth for
        "does this SKU have source history".
      - If link exists OR audit rows exist → 200 with `has_source`/`changes`
        accordingly populated.

    Filters: `since` is `created_at__gte`; `sources` whitelisted against
    `CHANGE_LOG_SOURCES`; `unseen_only=True` adds `applied_to_pim=False`.
    Returns at most `_MAX_TIMELINE` rows (newest first).
    """
    if not sku:
        raise ValueError("sku is required.")

    qs_all = SourceProductChangeLog.objects.filter(real_product_sku__iexact=sku)
    link = (
        SourceProductLink.objects.filter(real_product_sku__iexact=sku, is_active=True)
        .select_related("source")
        .order_by("-is_primary", "priority", "pk")
        .first()
    )

    if link is None and not qs_all.exists():
        raise ValueError(f"PIM SKU '{sku}' not found.")

    qs = qs_all
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if sources:
        invalid = [s for s in sources if s not in CHANGE_LOG_SOURCES]
        if invalid:
            raise ValueError(f"Unknown source value(s): {sorted(invalid)}.")
        qs = qs.filter(source__in=sources)
    if unseen_only:
        qs = qs.filter(applied_to_pim=False)

    rows = list(qs.select_related("triggered_by").order_by("-created_at", "-pk")[:_MAX_TIMELINE])

    unseen_count = qs_all.filter(applied_to_pim=False).count()
    last_change_at = qs_all.order_by("-created_at").values_list("created_at", flat=True).first()

    source_product_id = None
    source_payload = None
    if link is not None:
        source_payload = {"idx": link.source.idx, "name": link.source.name}
        sp_id = (
            SourceProduct.objects.filter(source=link.source, real_product__sku__iexact=sku)
            .values_list("id", flat=True)
            .first()
        )
        if sp_id is None:
            sp_id = (
                qs_all.filter(source_product__source=link.source).values_list("source_product_id", flat=True).first()
            )
        source_product_id = sp_id

    return {
        "real_product_sku": sku,
        "has_source": link is not None,
        "source_product_id": source_product_id,
        "source": source_payload,
        "unseen_count": unseen_count,
        "last_change_at": last_change_at,
        "changes": [_serialize_row(row) for row in rows],
    }


def _serialize_row(row: SourceProductChangeLog) -> dict[str, Any]:
    return {
        "id": row.pk,
        "source": row.source,
        "field_path": row.field_path,
        "before": row.before,
        "after": row.after,
        "created_at": row.created_at,
        "applied_to_pim": row.applied_to_pim,
        "applied_to_pim_at": row.applied_to_pim_at,
        "triggered_by": row.triggered_by.username if row.triggered_by_id else None,
    }


# ---------------------------------------------------------------------------
# Bulk has-changes lookup (PIM list view badge)
# ---------------------------------------------------------------------------


def bulk_has_changes(skus: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk badge lookup keyed by SKU. Two SELECTs total regardless of input size.

    Query 1 — `SourceProductLink` filtered by `real_product_sku__in` + active,
    annotated with `is_primary` and source idx/name. Picks the primary
    link per SKU (fallback: lowest pk).

    Query 2 — `SourceProductChangeLog` aggregated on `real_product_sku` with
    `unseen_count = COUNT(applied_to_pim=False)` and
    `last_change_at = MAX(created_at)`.

    Result: every requested SKU keys an entry, even with no source
    (`has_source=false, unseen_count=0`).
    """
    if not skus:
        return {}

    cleaned = [s for s in skus if s]
    if not cleaned:
        return {}

    links_qs = (
        SourceProductLink.objects.filter(real_product_sku__in=cleaned, is_active=True)
        .select_related("source")
        .order_by("-is_primary", "priority", "pk")
    )
    link_by_sku: dict[str, SourceProductLink] = {}
    for link in links_qs:
        if link.real_product_sku not in link_by_sku:
            link_by_sku[link.real_product_sku] = link

    agg_qs = (
        SourceProductChangeLog.objects.filter(real_product_sku__in=cleaned)
        .values("real_product_sku")
        .annotate(
            unseen_count=Count("id", filter=Q(applied_to_pim=False)),
            last_change_at=Max("created_at"),
            any_sp_id=Max("source_product_id"),
        )
    )
    agg_by_sku: dict[str, dict[str, Any]] = {row["real_product_sku"]: row for row in agg_qs}

    result: dict[str, dict[str, Any]] = {}
    for raw_sku in cleaned:
        link = link_by_sku.get(raw_sku)
        agg = agg_by_sku.get(raw_sku, {})
        result[raw_sku] = {
            "has_source": link is not None,
            "source_product_id": agg.get("any_sp_id"),
            "source_idx": link.source.idx if link else None,
            "unseen_count": agg.get("unseen_count", 0) or 0,
            "last_change_at": agg.get("last_change_at"),
        }
    return result


# ---------------------------------------------------------------------------
# Acknowledge (operator says "saw it, don't bother me")
# ---------------------------------------------------------------------------


def acknowledge(
    sku: str, *, change_ids: list[int] | None = None, all_unseen: bool = False, user: AbstractBaseUser | None = None
) -> int:
    """Mark unseen changes as applied_to_pim.

    Validates exactly-one mode (change_ids OR all_unseen). Bulk update in one
    UPDATE. Returns the number of rows transitioned False→True (idempotent
    — already-acknowledged rows in `change_ids` do not count toward the total).

    On non-zero ack: emit a single audit entry `source=operator_acknowledge`
    against the FIRST acknowledged log's `source_product` so the entry is
    attached to a concrete SP. Best-effort; failures are logged, never raised.
    """
    if not sku:
        raise ValueError("sku is required.")
    if change_ids is not None and all_unseen:
        raise ValueError("Specify exactly one of change_ids or all_unseen, not both.")
    if not change_ids and not all_unseen:
        raise ValueError("Specify exactly one of change_ids or all_unseen.")

    base_qs = SourceProductChangeLog.objects.filter(real_product_sku__iexact=sku, applied_to_pim=False)

    if change_ids is not None:
        if not change_ids:
            return 0
        valid_ids = list(base_qs.filter(pk__in=change_ids).values_list("pk", flat=True))
        unknown = set(change_ids) - set(valid_ids)
        if unknown:
            raise ValueError(f"change_ids {sorted(unknown)} do not belong to unseen changes for SKU '{sku}'.")
        target_qs = base_qs.filter(pk__in=valid_ids)
    else:
        target_qs = base_qs

    first_sp_id = target_qs.order_by("pk").values_list("source_product_id", "pk").first()
    if first_sp_id is None:
        return 0
    sp_id, _ = first_sp_id
    acknowledged_ids = list(target_qs.order_by("pk").values_list("pk", flat=True))

    now = timezone.now()
    updated = target_qs.update(applied_to_pim=True, applied_to_pim_at=now)

    if updated > 0:
        try:
            sp = SourceProduct.objects.get(pk=sp_id)
            audit_service.log_change(
                source_product=sp,
                source=ChangeLogSource.OPERATOR_ACKNOWLEDGE.value,
                field_path="acknowledge",
                before=None,
                after={"acknowledged_ids": acknowledged_ids, "count": updated},
                triggered_by=user,
                applied_to_pim=True,
                applied_to_pim_at=now,
                real_product_sku=sku,
            )
        except Exception:  # noqa: BLE001 — out-of-band, must not block ack mutation
            logger.warning("audit emit failed for acknowledge sku=%s sp=%s", sku, sp_id, exc_info=True)

    return updated


# ---------------------------------------------------------------------------
# Force re-push by SKU (convenience wrapper)
# ---------------------------------------------------------------------------


def force_repush_by_sku(sku: str, user: AbstractBaseUser | None) -> dict[str, Any]:
    """Find every active linked SP for the SKU, call push_service.force_repush
    on each. Returns a structured summary instead of raising on per-SP failures
    — operator sees partial-success when one SP is push-blocked (wrong status,
    failed preflight) but another succeeds.

    Zero active links → ValueError ("not found") → 404.
    """
    if not sku:
        raise ValueError("sku is required.")

    links = list(product_link_service.list_links(real_product_sku=sku, is_active=True))
    if not links:
        raise ValueError(f"No active source links for SKU '{sku}' not found.")

    processed_sp_ids: list[int] = []
    failed: list[dict[str, Any]] = []
    pushed_channels_count = 0

    for link in links:
        sp_id = (
            SourceProduct.objects.filter(source=link.source, real_product__sku__iexact=sku)
            .values_list("id", flat=True)
            .first()
        )
        if sp_id is None:
            failed.append({"source_idx": link.source.idx, "reason": "no SourceProduct row for this link"})
            continue
        try:
            products = push_service.force_repush_source_product(sp_id, user)
        except ValueError as exc:
            failed.append({"source_idx": link.source.idx, "sp_id": sp_id, "reason": str(exc)})
            continue
        processed_sp_ids.append(sp_id)
        pushed_channels_count += len(products)

    return {
        "sku": sku,
        "processed_sp_ids": processed_sp_ids,
        "pushed_channels_count": pushed_channels_count,
        "failed": failed,
    }
