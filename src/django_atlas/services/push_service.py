# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Push orchestrator — pre-flight + INIT push + force re-push (multi-channel, multi-profile).

Frozen decisions in scope:
  idempotent skip when channel_idx already in pushed_to_channel_idxs.
  push_source_product locks SP via select_for_update inside transaction.atomic().
"""

import traceback
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction

from django_atlas.enums import EventSeverity, EventType, ProductStatus
from django_atlas.models import Source, SourceMappingProfile, SourceProduct
from django_atlas.services import event_service, kind_guard, pim_writer

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _lock_sp(sp_id: int) -> SourceProduct:
    """Row-lock a SourceProduct by pk for the push flow . Raises ValueError, not DoesNotExist."""
    try:
        return SourceProduct.objects.select_for_update(of=("self",)).select_related("source", "feed").get(id=sp_id)
    except SourceProduct.DoesNotExist as exc:
        raise ValueError(f"SourceProduct with pk={sp_id} not found") from exc


def preflight_check(source: Source) -> dict[str, Any]:
    """Validate that a source is ready for push: active profiles, channels, features, categories.

    Returns {ok, errors, warnings, details}. errors → push must NOT proceed.
    """
    from django_pim.models.channel import Channel
    from django_pim.models.feature import Feature
    from django_pim.models.feature_set import FeatureSet
    from django_pim.models.product_category import ProductCategory

    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    # Monitoring sources are read-only observers — every push path funnels
    # through this preflight, so one gate here covers per-SP push, batch push,
    # bulk view, auto-push pipeline and force-repush-by-sku uniformly.
    if kind_guard.is_push_blocked(source):
        errors.append(kind_guard.PUSH_BLOCKED)
        return {"ok": False, "errors": errors, "warnings": warnings, "details": details}

    profiles = list(source.mapping_profiles.filter(is_active=True))
    if not profiles:
        errors.append("no_active_mapping_profile")
        return {"ok": False, "errors": errors, "warnings": warnings, "details": details}

    feature_set_idxs: set[str] = set()
    if source.default_feature_set_idx:
        feature_set_idxs.add(source.default_feature_set_idx)

    for profile in profiles:
        target_channels = list(profile.target_channel_idxs or [])
        if not target_channels:
            errors.append(f"profile_no_target_channels:{profile.idx}")
            continue
        if profile.feature_set_idx:
            feature_set_idxs.add(profile.feature_set_idx)
        for channel_idx in target_channels:
            if not Channel.objects.filter(idx=channel_idx).exists():
                errors.append(f"pim_channel_missing:{channel_idx}")

        for mapping in profile.attribute_mappings.filter(target_type="feature"):
            if not Feature.objects.filter(idx=mapping.target_identifier).exists():
                errors.append(f"pim_feature_missing:{mapping.target_identifier}")

        for mapping in profile.category_mappings.all():
            matched_channels: list[str] = []
            missing_channels: list[str] = []
            for channel_idx in target_channels:
                if ProductCategory.objects.filter(shop__idx=channel_idx, idx=mapping.target_category_idx).exists():
                    matched_channels.append(channel_idx)
                else:
                    missing_channels.append(channel_idx)
            if not matched_channels:
                errors.append(f"pim_category_missing_all_channels:{mapping.target_category_idx}")
            elif missing_channels:
                warnings.append(
                    f"pim_category_missing_in_some_channels:{mapping.target_category_idx} (missing: {missing_channels})"
                )

    for fs_idx in feature_set_idxs:
        if not FeatureSet.objects.filter(idx=fs_idx).exists():
            errors.append(f"pim_feature_set_missing:{fs_idx}")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "details": details}


# ---------------------------------------------------------------------------
# Push (per-SP)
# ---------------------------------------------------------------------------


def _decide_post_push_status(sp: SourceProduct) -> str:
    if sp.image_urls:
        return ProductStatus.PUSHED_PENDING_IMAGES.value
    return ProductStatus.PUSHED.value


def _active_profiles_for(source: Source) -> list[SourceMappingProfile]:
    """Defensive re-read per push to respect concurrent profile is_active toggles."""
    return list(source.mapping_profiles.filter(is_active=True))


def push_source_product(
    sp_id: int, user: AbstractBaseUser | None, *, event_sink: list[dict[str, Any]] | None = None
) -> list:
    """Push a single SP to all configured (profile × channel) combinations.

    Caller MUST wrap in transaction.atomic() — select_for_update requires it.

    `event_sink` (optional): when caller wants per-push warning events surfaced back, pass a list — pim_writer appends dicts.
    """
    sp = _lock_sp(sp_id)

    if sp.status != ProductStatus.APPROVED.value:
        raise ValueError(f"SourceProduct {sp.id} is in status '{sp.status}', expected 'approved' to push")

    preflight = preflight_check(sp.source)
    if not preflight["ok"]:
        raise ValueError(f"Pre-flight failed for source {sp.source.idx}: {preflight['errors']}")

    return _push_source_product_trusted(sp, user, event_sink=event_sink)


def _push_source_product_trusted(
    sp: SourceProduct, user: AbstractBaseUser | None, *, event_sink: list[dict[str, Any]] | None = None
) -> list:
    """Per-SP push core, assumes preflight already validated by caller (perf hot path).

    Used by push_approved_for_source batch loop to avoid re-running PIM EXISTS queries
    once per SP (saves N×preflight queries). Public path push_source_product still runs
    the guard via the wrapper above.
    """
    products: list = []
    pushed_set = set(sp.pushed_to_channel_idxs or [])
    profiles = _active_profiles_for(sp.source)

    for profile in profiles:
        for channel_idx in profile.target_channel_idxs or []:
            if channel_idx in pushed_set:
                continue
            product = pim_writer.init_push_to_channel(sp, profile, channel_idx, user, event_sink=event_sink)
            products.append(product)
            pushed_set.add(channel_idx)

    new_status = _decide_post_push_status(sp)
    if sp.status != new_status:
        sp.status = new_status
        sp.save(update_fields=["status", "modified_at"])

    event_service.record(
        event_type=EventType.PUSH_SUCCEEDED.value,
        severity=EventSeverity.INFO.value,
        source=sp.source,
        source_product=sp,
        message=f"Pushed SP {sp.id} to {len(products)} channel(s)",
        details={"channels": list(sp.pushed_to_channel_idxs or [])},
    )

    return products


def push_approved_for_source(source_id: int, user: AbstractBaseUser | None = None) -> dict[str, Any]:
    """Push every approved SP for a source. Per-SP atomic; failures isolated.

    Returns {success, failed, preflight_failed, errors}.
    """
    source = Source.objects.get(id=source_id)
    preflight = preflight_check(source)
    if not preflight["ok"]:
        return {"success": 0, "failed": 0, "preflight_failed": True, "errors": preflight["errors"]}

    success = 0
    failed = 0
    approved_qs = SourceProduct.objects.filter(source_id=source_id, status=ProductStatus.APPROVED.value).order_by("id")

    for sp in approved_qs.only("id"):
        try:
            with transaction.atomic():
                # Trusted path — preflight already validated above. Re-acquire row lock per SP.
                locked_sp = (
                    SourceProduct.objects.select_for_update(of=("self",)).select_related("source", "feed").get(id=sp.id)
                )
                if locked_sp.status != ProductStatus.APPROVED.value:
                    raise ValueError(f"SourceProduct {sp.id} no longer approved (status={locked_sp.status})")
                _push_source_product_trusted(locked_sp, user)
            success += 1
        except Exception as exc:  # noqa: BLE001 — source-scoped flow, isolate failures per-SP
            failed += 1
            event_service.record(
                event_type=EventType.PUSH_FAILED.value,
                severity=EventSeverity.WARNING.value,
                source=source,
                source_product=SourceProduct.objects.filter(id=sp.id).first(),
                message=f"Push failed for SP {sp.id}: {exc}",
                details={"traceback": traceback.format_exc()[-2000:]},
            )

    return {"success": success, "failed": failed, "preflight_failed": False, "errors": []}


# ---------------------------------------------------------------------------
# Force re-push
# ---------------------------------------------------------------------------


def force_repush_source_product(
    sp_id: int, user: AbstractBaseUser | None, *, event_sink: list[dict[str, Any]] | None = None
) -> list:
    """Force re-push: overwrite attributes/categories/physical/images for already-pushed channels.

    Status flow (bypasses STATUS_TRANSITIONS — this is an internal force flow):
      pushed/pushed_pending_images → approved → pushed/pushed_pending_images

    `event_sink` (optional): same contract as push_source_product.
    """
    from django.utils import timezone

    with transaction.atomic():
        sp = _lock_sp(sp_id)

        allowed = {ProductStatus.PUSHED.value, ProductStatus.PUSHED_PENDING_IMAGES.value}
        if sp.status not in allowed:
            raise ValueError(
                f"SourceProduct {sp.id} is in status '{sp.status}', force re-push requires 'pushed' or 'pushed_pending_images'"
            )

        preflight = preflight_check(sp.source)
        if not preflight["ok"]:
            raise ValueError(f"Pre-flight failed for source {sp.source.idx}: {preflight['errors']}")

        # Audit reset: pushed/pushed_pending_images → approved (force re-push intent).
        # Bypasses STATUS_TRANSITIONS — pushed_pending_images→approved is not normally allowed.
        sp.status = ProductStatus.APPROVED.value
        sp.reviewed_at = timezone.now()
        sp.save(update_fields=["status", "reviewed_at", "modified_at"])

        products: list = []
        pushed_channels = list(sp.pushed_to_channel_idxs or [])
        profiles = _active_profiles_for(sp.source)

        for profile in profiles:
            for channel_idx in profile.target_channel_idxs or []:
                if channel_idx not in pushed_channels:
                    continue
                product = pim_writer.force_repush_to_channel(sp, profile, channel_idx, user, event_sink=event_sink)
                products.append(product)

        # Status back: pushed_pending_images if image_urls non-empty (pictures wiped), else pushed.
        new_status = _decide_post_push_status(sp)
        sp.status = new_status
        sp.save(update_fields=["status", "modified_at"])

        event_service.record(
            event_type=EventType.FORCE_REPUSH_EXECUTED.value,
            severity=EventSeverity.INFO.value,
            source=sp.source,
            source_product=sp,
            message=f"Force re-pushed SP {sp.id} to {len(products)} channel(s)",
            details={"channels": pushed_channels},
        )

    return products


__all__ = ["force_repush_source_product", "preflight_check", "push_approved_for_source", "push_source_product"]
