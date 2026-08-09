# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Review workflow service for SourceProduct.

Status workflow (README §8.9). Audit fields (`reviewed_by`, `reviewed_at`)
are set when status moves through review stages, with one exception:

  - `pushed → approved` (force re-push intent): refresh `reviewed_at` only,
    keep original `reviewed_by`. The pusher gets attribution via
    `pushed_by` after the actual push

  - `bulk_requeue` (decision #29, `rejected → queued`): keep `reviewed_by`
    and `reviewed_at` untouched. Re-queue is a rollback of the reject
    intent, NOT a new review event.
"""

from decimal import Decimal
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from django_atlas.enums import ProductStatus
from django_atlas.models import SourceProduct
from django_atlas.services import kind_guard, product_service

_ORDERING_FIELDS = frozenset({"data_changed_at", "created_at", "modified_at", "cost", "name", "status"})


def _validate_ordering(ordering: str) -> str:
    field = ordering[1:] if ordering.startswith("-") else ordering
    if field not in _ORDERING_FIELDS:
        raise ValueError(f"Invalid ordering field: {field!r}. Allowed: {sorted(_ORDERING_FIELDS)}")
    return ordering


STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    ProductStatus.NEW.value: frozenset(
        {ProductStatus.QUEUED.value, ProductStatus.APPROVED.value, ProductStatus.REJECTED.value}
    ),
    ProductStatus.QUEUED.value: frozenset(
        {ProductStatus.NEW.value, ProductStatus.APPROVED.value, ProductStatus.REJECTED.value}
    ),
    ProductStatus.APPROVED.value: frozenset({ProductStatus.REJECTED.value, ProductStatus.PUSHED_PENDING_IMAGES.value}),
    ProductStatus.REJECTED.value: frozenset({ProductStatus.QUEUED.value}),
    ProductStatus.PUSHED_PENDING_IMAGES.value: frozenset({ProductStatus.PUSHED.value}),
    ProductStatus.PUSHED.value: frozenset({ProductStatus.APPROVED.value}),
}

_REVIEW_AUDIT_STATUSES = frozenset(
    {ProductStatus.APPROVED.value, ProductStatus.REJECTED.value, ProductStatus.QUEUED.value}
)

# Monitoring sources never push, so approve ("push next") and queue (push triage)
# are meaningless for them. Reject/skip stay available.
_MONITORING_BLOCKED_TARGETS = frozenset({ProductStatus.APPROVED.value, ProductStatus.QUEUED.value})


def transition_status(sp: SourceProduct, new_status: str, *, user: AbstractBaseUser | None = None) -> SourceProduct:
    allowed = STATUS_TRANSITIONS.get(sp.status)
    if allowed is None or new_status not in allowed:
        raise ValueError(f"invalid transition: {sp.status} -> {new_status}")
    if new_status in _MONITORING_BLOCKED_TARGETS:
        kind_guard.assert_push_allowed(sp.source, f"review transition to '{new_status}'")

    pushed_to_approved = sp.status == ProductStatus.PUSHED.value and new_status == ProductStatus.APPROVED.value
    sp.status = new_status

    if pushed_to_approved:
        sp.reviewed_at = timezone.now()
    elif new_status in _REVIEW_AUDIT_STATUSES:
        sp.reviewed_by = user
        sp.reviewed_at = timezone.now()

    sp.save()
    return sp


def approve(sp_id: int, user: AbstractBaseUser | None) -> SourceProduct:
    return transition_status(product_service.get_sp(sp_id), ProductStatus.APPROVED.value, user=user)


def reject(sp_id: int, user: AbstractBaseUser | None) -> SourceProduct:
    return transition_status(product_service.get_sp(sp_id), ProductStatus.REJECTED.value, user=user)


def skip(sp_id: int, user: AbstractBaseUser | None) -> SourceProduct:  # noqa: ARG001 — accepted for API symmetry
    sp = product_service.get_sp(sp_id)
    if sp.status != ProductStatus.QUEUED.value:
        raise ValueError(f"skip only valid from queued, got {sp.status}")
    sp.status = ProductStatus.NEW.value
    sp.save(update_fields=["status", "modified_at"])
    return sp


def queue(sp_id: int, user: AbstractBaseUser | None) -> SourceProduct:
    return transition_status(product_service.get_sp(sp_id), ProductStatus.QUEUED.value, user=user)


def _bulk_apply(
    sp_ids: list[int], target_status: str, *, user: AbstractBaseUser | None, touch_audit: bool
) -> dict[str, Any]:
    """Validate transitions per-row, then apply via single .update() (perf #3)."""
    valid_ids: list[int] = []
    invalid = 0
    failed: list[int] = []
    gate_roles = target_status in _MONITORING_BLOCKED_TARGETS
    qs = SourceProduct.objects.filter(pk__in=sp_ids).only("id", "status")
    if gate_roles:
        qs = qs.select_related("source").only("id", "status", "source__kind", "source__idx")
    for sp in qs:
        allowed = STATUS_TRANSITIONS.get(sp.status)
        if allowed is None or target_status not in allowed or (gate_roles and kind_guard.is_push_blocked(sp.source)):
            invalid += 1
            failed.append(sp.pk)
            continue
        valid_ids.append(sp.pk)

    if valid_ids:
        update_fields: dict[str, Any] = {"status": target_status, "modified_at": timezone.now()}
        if touch_audit:
            update_fields["reviewed_by"] = user
            update_fields["reviewed_at"] = timezone.now()
        SourceProduct.objects.filter(pk__in=valid_ids).update(**update_fields)
    return {"success": len(valid_ids), "invalid_transition": invalid, "ids_failed": failed}


def bulk_approve(sp_ids: list[int], user: AbstractBaseUser | None) -> dict[str, Any]:
    return _bulk_apply(sp_ids, ProductStatus.APPROVED.value, user=user, touch_audit=True)


def bulk_reject(sp_ids: list[int], user: AbstractBaseUser | None) -> dict[str, Any]:
    return _bulk_apply(sp_ids, ProductStatus.REJECTED.value, user=user, touch_audit=True)


def bulk_requeue(sp_ids: list[int], user: AbstractBaseUser | None) -> dict[str, Any]:  # noqa: ARG001 — user accepted for API symmetry, audit preserved
    """Bulk transition rejected -> queued, preserving original reviewed_by (decision #29).

    Perf #3: single bulk .update() instead of per-row .save().
    """
    valid_ids: list[int] = []
    invalid = 0
    failed: list[int] = []
    qs = (
        SourceProduct.objects.filter(pk__in=sp_ids)
        .select_related("source")
        .only("id", "status", "source__kind", "source__idx")
    )
    for sp in qs:
        if sp.status != ProductStatus.REJECTED.value or kind_guard.is_push_blocked(sp.source):
            invalid += 1
            failed.append(sp.pk)
            continue
        valid_ids.append(sp.pk)

    if valid_ids:
        SourceProduct.objects.filter(pk__in=valid_ids).update(
            status=ProductStatus.QUEUED.value, modified_at=timezone.now()
        )
    return {"success": len(valid_ids), "invalid_transition": invalid, "ids_failed": failed}


def list_for_review(
    *,
    source_id: int | None = None,
    status: str = ProductStatus.QUEUED.value,
    search: str | None = None,
    ean: str | None = None,
    cost_min: Decimal | None = None,
    cost_max: Decimal | None = None,
    ordering: str = "-data_changed_at",
) -> QuerySet[SourceProduct]:
    # denormalise real_product_sku + source_idx/name into the row so the
    # response schema can expose them without N+1 (bulk acknowledge needs the SKU,
    # UpdatedMode per-source sidebar needs idx/name).
    qs = SourceProduct.objects.annotate(
        real_product_sku=F("real_product__sku"),
        source_idx=F("source__idx"),
        source_name=F("source__name"),
        kind=F("source__kind"),
    )
    if status is not None:
        qs = qs.filter(status=status)
    if source_id is not None:
        qs = qs.filter(source_id=source_id)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(external_id__icontains=search))
    if ean:
        qs = qs.filter(ean=ean)
    if cost_min is not None:
        qs = qs.filter(cost__gte=cost_min)
    if cost_max is not None:
        qs = qs.filter(cost__lte=cost_max)
    return qs.order_by(_validate_ordering(ordering))
