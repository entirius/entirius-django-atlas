# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-field audit log writer for SourceProduct.

Mirror of `event_service` shape (record / record_bulk / prune_older_than).
Service-layer whitelist on `source` (`CHANGE_LOG_SOURCES`) — write paths pass a
member of `ChangeLogSource.values`; anything else raises `ValueError`.

Bulk path used by sync write paths (`process_full_sync`, `process_delta_sync`)
to keep N+1 off the table; `batch_size=500` matches `IntegrationEvent`.

Out-of-band semantics: callers are expected to wrap log calls in try/except
inside the main flow (sync/push). Failures here NEVER block the data path —
the audit log is best-effort observability, not a transactional dependency.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

from django_atlas.enums import CHANGE_LOG_SOURCES
from django_atlas.models import SourceProduct, SourceProductChangeLog

_BATCH_SIZE = 500


def _json_safe(value: Any) -> Any:
    """Convert Decimal → str so JSONField stores stable shape (Postgres + SQLite parity).

    Recursive on lists/dicts. Acts as defense-in-depth even when callers pre-sanitize.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _validate(*, source: str, field_path: str) -> None:
    if source not in CHANGE_LOG_SOURCES:
        raise ValueError(f"source '{source}' is not in whitelist (enums.ChangeLogSource).")
    if not field_path:
        raise ValueError("field_path is required.")


def log_change(
    *,
    source_product: SourceProduct,
    source: str,
    field_path: str,
    before: Any = None,
    after: Any = None,
    triggered_by: AbstractBaseUser | None = None,
    applied_to_pim: bool = False,
    applied_to_pim_at: datetime | None = None,
    real_product_sku: str | None = None,
) -> SourceProductChangeLog:
    """Single-entry write. Use for low-volume paths (operator edits, push baselines)."""
    _validate(source=source, field_path=field_path)
    if applied_to_pim and applied_to_pim_at is None:
        applied_to_pim_at = timezone.now()
    resolved_sku = real_product_sku
    if resolved_sku is None and source_product.real_product_id is not None:
        resolved_sku = source_product.real_product.sku
    return SourceProductChangeLog.objects.create(
        source_product=source_product,
        real_product_sku=resolved_sku or "",
        source=source,
        field_path=field_path,
        before=_json_safe(before),
        after=_json_safe(after),
        triggered_by=triggered_by,
        applied_to_pim=applied_to_pim,
        applied_to_pim_at=applied_to_pim_at,
    )


def log_changes_bulk(drafts: list[dict[str, Any]]) -> int:
    """Bulk write — drafts is a list of kwargs dicts matching `log_change` params.

    Validation runs per-draft before any INSERT; one bad draft aborts the batch
    so the caller learns about it immediately rather than silently losing data.
    """
    if not drafts:
        return 0
    instances: list[SourceProductChangeLog] = []
    now = timezone.now()
    for index, draft in enumerate(drafts):
        try:
            sp = draft["source_product"]
            source = draft["source"]
            field_path = draft["field_path"]
        except KeyError as exc:
            raise ValueError(f"drafts[{index}] missing required key: {exc.args[0]}") from exc
        try:
            _validate(source=source, field_path=field_path)
        except ValueError as exc:
            raise ValueError(f"drafts[{index}] invalid: {exc}") from exc
        applied = draft.get("applied_to_pim", False)
        applied_at = draft.get("applied_to_pim_at")
        if applied and applied_at is None:
            applied_at = now
        resolved_sku = draft.get("real_product_sku")
        if resolved_sku is None and sp.real_product_id is not None:
            resolved_sku = sp.real_product.sku
        instances.append(
            SourceProductChangeLog(
                source_product=sp,
                real_product_sku=resolved_sku or "",
                source=source,
                field_path=field_path,
                before=_json_safe(draft.get("before")),
                after=_json_safe(draft.get("after")),
                triggered_by=draft.get("triggered_by"),
                applied_to_pim=applied,
                applied_to_pim_at=applied_at,
            )
        )
    SourceProductChangeLog.objects.bulk_create(instances, batch_size=_BATCH_SIZE)
    return len(instances)


def prune_older_than(days: int) -> int:
    """Delete audit log entries older than `days`. Returns deleted row count.

    No severity-based exclusion (unlike `event_service.prune_older_than`) —
    audit log entries are uniform: an old before/after is an old before/after.
    """
    if days < 0:
        raise ValueError("days must be >= 0.")
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = SourceProductChangeLog.objects.filter(created_at__lt=cutoff).delete()
    return deleted
