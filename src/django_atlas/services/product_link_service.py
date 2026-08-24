# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SourceProductLink CRUD + auto-upsert from push.

Frozen decisions:
  - multi-source per PIM product via SourceProductLink.
  - manual scenario: no SourceProduct, link added manually by PIM SKU.
  - `is_primary` is operator-only. `upsert_for_push` MUST never overwrite
    is_primary / priority / notes / is_active — a re-import must not reset
    operator flags.
"""

import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import QuerySet

from django_atlas.models import Source, SourceProductLink

if TYPE_CHECKING:  # annotations only — both models are imported lazily inside the functions
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProduct

logger = logging.getLogger(__name__)

_EDITABLE_FIELDS = frozenset({"external_id", "priority", "is_primary", "is_active", "notes"})


def list_links(
    *, real_product_sku: str | None = None, source_id: int | None = None, is_active: bool | None = None
) -> QuerySet[SourceProductLink]:
    qs = SourceProductLink.objects.all()
    if real_product_sku is not None:
        qs = qs.filter(real_product_sku__iexact=real_product_sku)
    if source_id is not None:
        qs = qs.filter(source_id=source_id)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    return qs


def get_link(pk: int) -> SourceProductLink:
    try:
        return SourceProductLink.objects.get(pk=pk)
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(f"SourceProductLink with pk={pk} not found") from exc


def get_primary_source_idx(real_product_sku: str) -> str | None:
    """Current primary source idx for a SKU, or None. Read-only lookup for API responses."""
    link = (
        SourceProductLink.objects.filter(real_product_sku=real_product_sku, is_primary=True)
        .select_related("source")
        .first()
    )
    return link.source.idx if link else None


def has_active_link(real_product_sku: str) -> bool:
    return SourceProductLink.objects.filter(real_product_sku=real_product_sku, is_active=True).exists()


def create_link(
    real_product_sku: str,
    source_idx: str,
    *,
    external_id: str = "",
    priority: int = 0,
    is_primary: bool = False,
    notes: str = "",
) -> SourceProductLink:
    """Create a manual link. Validates SKU exists in PIM and (sku, source) is unique."""
    from django_pim.models.real_product import RealProduct

    if not RealProduct.objects.filter(sku__iexact=real_product_sku).exists():
        raise ValueError(f"PIM RealProduct with SKU '{real_product_sku}' not found")

    try:
        source = Source.objects.get(idx=source_idx)
    except Source.DoesNotExist as exc:
        raise ValueError(f"Source '{source_idx}' not found") from exc

    if SourceProductLink.objects.filter(real_product_sku=real_product_sku, source=source).exists():
        raise ValueError(f"SourceProductLink already exists for sku='{real_product_sku}' source='{source_idx}'")

    if is_primary:
        from django_atlas.services import kind_guard

        kind_guard.assert_push_allowed(source, "creating a primary link")

    with transaction.atomic():
        if is_primary:
            SourceProductLink.objects.filter(real_product_sku=real_product_sku).update(is_primary=False)
        return SourceProductLink.objects.create(
            real_product_sku=real_product_sku,
            source=source,
            external_id=external_id,
            priority=priority,
            is_primary=is_primary,
            notes=notes,
        )


def update_link(pk: int, **fields: Any) -> SourceProductLink:
    """Update a link. Only whitelisted fields editable; `real_product_sku`/`source` immutable."""
    invalid = set(fields) - _EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_link: {sorted(invalid)}")
    try:
        link = SourceProductLink.objects.select_related("source").get(pk=pk)
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(f"SourceProductLink with pk={pk} not found") from exc
    if fields.get("is_primary"):
        from django_atlas.services import kind_guard

        kind_guard.assert_push_allowed(link.source, "setting a link as primary")
        with transaction.atomic():
            SourceProductLink.objects.filter(real_product_sku=link.real_product_sku).exclude(pk=pk).update(
                is_primary=False
            )
            for field, value in fields.items():
                setattr(link, field, value)
            link.save()
        return link
    for field, value in fields.items():
        setattr(link, field, value)
    link.save()
    return link


def delete_link(pk: int) -> None:
    SourceProductLink.objects.filter(pk=pk).delete()


def upsert_for_push(
    real_product_sku: str, source: Source, external_id: str | None = None, *, set_primary_if_first: bool = False
) -> SourceProductLink:
    """Push-side upsert: create if missing, update only `external_id`.

    defaults MUST contain ONLY external_id on the upsert. Never reset is_primary /
    priority / notes / is_active on UPDATE (operator-controlled fields). Re-imports must
    not erase operator state.

    `set_primary_if_first` — when True AND the link was just created AND no
    other link exists for this SKU, set is_primary=True. That contract is preserved because we
    only touch is_primary at creation time (nothing to overwrite) and only when this
    is the only link for the SKU. Subsequent re-imports never re-flag because the link
    already exists. The legacy single-source flow finally gets a primary link by
    default — required so the cost subscriber stops ignoring it.
    """
    defaults = {"external_id": external_id or ""}
    link, created = SourceProductLink.objects.update_or_create(
        real_product_sku=real_product_sku, source=source, defaults=defaults
    )
    if created and set_primary_if_first:
        other_links_exist = (
            SourceProductLink.objects.filter(real_product_sku=real_product_sku).exclude(pk=link.pk).exists()
        )
        if not other_links_exist:
            from django_atlas.services import kind_guard

            if not kind_guard.is_push_blocked(source):
                link.is_primary = True
                link.save(update_fields=["is_primary", "modified_at"])
    return link


def set_primary(pk: int) -> SourceProductLink:
    """Mark a link as the primary source for its SKU. Unsets all other links for the same SKU."""
    try:
        link = SourceProductLink.objects.select_related("source").get(pk=pk)
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(f"SourceProductLink with pk={pk} not found") from exc
    from django_atlas.services import kind_guard

    kind_guard.assert_push_allowed(link.source, "setting a link as primary")
    with transaction.atomic():
        SourceProductLink.objects.filter(real_product_sku=link.real_product_sku).exclude(pk=pk).update(is_primary=False)
        link.is_primary = True
        link.save(update_fields=["is_primary", "modified_at"])
    return link


def unset_primary(pk: int) -> SourceProductLink:
    """Clear is_primary flag. Post-state may be 'no primary for SKU' (operator decides)."""
    try:
        link = SourceProductLink.objects.get(pk=pk)
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(f"SourceProductLink with pk={pk} not found") from exc
    link.is_primary = False
    link.save(update_fields=["is_primary", "modified_at"])
    return link


def link_sp_to_realproduct(
    sp_pk: int,
    real_product_sku: str,
    user: AbstractBaseUser | None,
    *,
    event_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Operator link ("find in PIM" box): attach an unlinked SP to an EXISTING RealProduct.

    Inverse of `unlink_sp_from_realproduct`, and the reason a bare `create_link` is not enough:
    only `SourceProduct.real_product` takes the SP out of the dedup candidate pool
    (`lookup_provider.candidates()` serves `real_product IS NULL`), so the attach and the
    `SourceProductLink` must happen together or the row keeps being re-proposed and a later
    auto-push spawns a duplicate RealProduct.

    Links to an EXISTING RealProduct are allowed for every `Source.kind` (no PIM row is created),
    so no `kind_guard` gate here — `is_primary` stays False and is the operator's separate call.

    Side effects: IntegrationEvent `linked_via_lookup_ui` (also appended to `event_sink`) and a
    best-effort `accepted` verdict in django-lookup's dedup log (score 0 — a manual link carries
    no machine score).

    Raises ValueError when the SP or the SKU does not exist, or when the SP is already linked.
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProduct

    try:
        sp = SourceProduct.objects.select_related("source", "real_product").get(pk=sp_pk)
    except SourceProduct.DoesNotExist as exc:
        raise ValueError(f"SourceProduct with pk={sp_pk} not found") from exc
    if sp.real_product_id is not None:
        raise ValueError(
            f"SourceProduct {sp_pk} is already linked to RealProduct '{sp.real_product.sku}' — unlink it first"
        )
    real_product = RealProduct.objects.filter(sku__iexact=real_product_sku).first()
    if real_product is None:
        raise ValueError(f"PIM RealProduct with SKU '{real_product_sku}' not found")

    link = _attach_sp(sp, real_product, event_sink)
    try:
        _record_link_verdict(sp, real_product.sku, user)
    except (ImportError, RuntimeError):
        # django-lookup is a soft dep — its absence must never block a link. RuntimeError
        # alongside ImportError matches qms_writer._qms_available's own precedent: Django
        # raises RuntimeError (not ImportError) when a module is on PYTHONPATH but not in
        # INSTALLED_APPS — the common multi-repo dev-checkout shape, not a real failure. Any
        # OTHER exception (an actual dedup_log bug, a DB error) is the operator's verdict
        # getting lost silently, so it must surface instead of being swallowed here.
        logger.warning("django-lookup not installed — dedup_log.record skipped for link_to_realproduct")
    return {"real_product_sku": real_product.sku, "link_pk": link.pk}


def _attach_sp(
    sp: "SourceProduct", real_product: "RealProduct", event_sink: list[dict[str, Any]] | None
) -> SourceProductLink:
    """One transaction: SP -> RealProduct, the (sku, source) link, and the audit event."""
    from django_atlas.enums import EventSeverity, EventType
    from django_atlas.services import event_service

    message = f"SP {sp.id} linked to RealProduct {real_product.sku} from the lookup UI"
    details = {
        "source_product_id": sp.id,
        "source_idx": sp.source.idx,
        "real_product_sku": real_product.sku,
    }
    with transaction.atomic():
        link, _ = SourceProductLink.objects.get_or_create(
            real_product_sku=real_product.sku, source=sp.source, defaults={"external_id": sp.external_id}
        )
        sp.real_product = real_product
        sp.save(update_fields=["real_product", "modified_at"])
        event_service.record(
            event_type=EventType.LINKED_VIA_LOOKUP_UI.value,
            severity=EventSeverity.INFO.value,
            source=sp.source,
            source_product=sp,
            message=message,
            details=details,
        )
    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.LINKED_VIA_LOOKUP_UI.value,
                "severity": EventSeverity.INFO.value,
                "message": message,
                "details": details,
            }
        )
    return link


def _record_link_verdict(sp: "SourceProduct", real_product_sku: str, user: AbstractBaseUser | None) -> None:
    """Log the operator's "same product" answer in django-lookup (calibration + never re-propose).

    Soft-dependency touchpoint, faked in the tests exactly like the enrichment adapter's own.
    """
    from django_lookup.services import dedup_log

    from django_atlas.services import enrichment_adapter, lookup_provider

    dedup_log.record(
        dedup_log.Verdict(
            subject_ref=lookup_provider.ref_for(sp),
            candidate_kind=enrichment_adapter.PIM_KIND,
            candidate_ref=real_product_sku,
            decision_human=enrichment_adapter.DECISION_ACCEPTED,
            decision_auto="",
            score=0,
        ),
        user=user,
    )


def unlink_sp_from_realproduct(
    sp_pk: int, user: AbstractBaseUser | None, *, event_sink: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    """Operator force-unlink: detach an auto-linked SP from its RealProduct.

    Creates a fresh RealProduct (per-source-prefixed SKU via pim_writer.generate_sku),
    moves the SP to it, deletes the old SourceProductLink, and creates a new one
    flagged is_primary=True (the SP is now the only source on the new RP).

    The original RealProduct is left intact — other sources' SourceProductLinks
    may still reference it.

    Side effects:
      - audit row source=manual_unlink with before/after sku
      - IntegrationEvent manual_unlink_from_realproduct (info) appended to event_sink
      - all DB writes wrapped in transaction.atomic()

    Raises ValueError when sp is missing, or when sp has no real_product / no link.
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.enums import ChangeLogSource, EventSeverity, EventType
    from django_atlas.models import SourceProduct
    from django_atlas.services import audit_service, event_service, pim_writer

    try:
        sp = SourceProduct.objects.select_related("source", "real_product").get(pk=sp_pk)
    except SourceProduct.DoesNotExist as exc:
        raise ValueError(f"SourceProduct with pk={sp_pk} not found") from exc

    if sp.real_product_id is None:
        raise ValueError(f"SourceProduct {sp_pk} is not linked to any RealProduct")

    source = sp.source
    from django_atlas.services import kind_guard

    # Unlink creates a FRESH RealProduct for the SP — a PIM write monitoring may not do.
    kind_guard.assert_push_allowed(source, "unlink (creates a new RealProduct)")
    old_rp = sp.real_product
    old_sku = old_rp.sku

    try:
        link = SourceProductLink.objects.get(real_product_sku=old_sku, source=source)
    except SourceProductLink.DoesNotExist as exc:
        raise ValueError(
            f"SourceProduct {sp_pk} is attached to RealProduct '{old_sku}' but no "
            f"SourceProductLink exists for (sku='{old_sku}', source='{source.idx}')"
        ) from exc

    new_sku = pim_writer.generate_sku(source, sp.external_id)
    if new_sku == old_sku or RealProduct.objects.filter(sku=new_sku).exists():
        raise ValueError(
            f"Cannot unlink: generated SKU '{new_sku}' already exists. "
            "Pick a different external_id or manually adjust the SP before unlinking."
        )

    with transaction.atomic():
        new_rp = RealProduct.objects.create(
            sku=new_sku,
            ean=sp.ean or None,
            weight=old_rp.weight,
            width=old_rp.width,
            height=old_rp.height,
            deep=old_rp.deep,
            kind_of_product=old_rp.kind_of_product,
        )
        link.delete()
        new_link = SourceProductLink.objects.create(
            real_product_sku=new_sku, source=source, external_id=sp.external_id, is_primary=True
        )
        sp.real_product = new_rp
        sp.save(update_fields=["real_product", "modified_at"])

    message = f"SP {sp.id} unlinked from RealProduct '{old_sku}' and reattached to fresh RealProduct '{new_sku}'."
    details = {
        "source_product_id": sp.id,
        "source_idx": source.idx,
        "previous_real_product_sku": old_sku,
        "new_real_product_sku": new_sku,
        "ean": sp.ean or None,
    }
    try:
        event_service.record(
            event_type=EventType.MANUAL_UNLINK_FROM_REALPRODUCT.value,
            severity=EventSeverity.INFO.value,
            source=source,
            source_product=sp,
            message=message,
            details=details,
        )
    except Exception:  # noqa: BLE001 — event log must be best-effort
        logger.warning("event_service.record failed for manual_unlink_from_realproduct", exc_info=True)

    try:
        audit_service.log_change(
            source_product=sp,
            source=ChangeLogSource.MANUAL_UNLINK.value,
            field_path="real_product.unlink",
            before={"sku": old_sku},
            after={"sku": new_sku, "ean": sp.ean or None},
            triggered_by=user,
            applied_to_pim=True,
            real_product_sku=new_sku,
        )
    except Exception:  # noqa: BLE001 — audit must be best-effort
        logger.warning("audit_service.log_change failed for manual_unlink", exc_info=True)

    if event_sink is not None:
        event_sink.append(
            {
                "event_type": EventType.MANUAL_UNLINK_FROM_REALPRODUCT.value,
                "severity": EventSeverity.INFO.value,
                "message": message,
                "details": details,
            }
        )

    return {"previous_real_product_sku": old_sku, "new_real_product_sku": new_sku, "new_link_pk": new_link.pk}
