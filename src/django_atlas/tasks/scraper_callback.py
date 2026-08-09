# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Celery task: process scraper-workers async results into ImportLog.

Consumes queue `atlas_results` (decyzja arch. — separate from default).
Pydantic-validates each item, finalizes ImportLog via `import_service.process_feed_results`,
emits `IntegrationEvent` for validation errors / unknown run_id / duplicate runs.

When `ATLAS_SCRAPER_CALLBACK_HMAC_SECRET` is configured, payload signature is
verified before any DB work — mismatched/missing signatures get logged as a
`scraper_callback_signature_invalid` event and the task returns early.
"""

import hashlib
import hmac
import json
from uuid import UUID

from celery import shared_task
from pydantic import ValidationError

from django_atlas import settings as source_settings
from django_atlas.enums import EventSeverity, EventType, LogStatus
from django_atlas.models import ImportLog
from django_atlas.schemas.contract import RawProduct
from django_atlas.services import event_service, import_service, log_service
from django_atlas.settings import QUEUE_RESULTS


def compute_signature(run_id: str, raw_products: list[dict], secret: str) -> str:
    """HMAC-SHA256 over `run_id + canonical_json(raw_products)` — matches the worker side."""
    canonical = json.dumps(raw_products, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = run_id.encode("utf-8") + b"|" + canonical
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _verify_signature(run_id: str, raw_products: list[dict], signature: str | None) -> bool:
    """Return True if signing is disabled OR signature matches. False = reject task."""
    secret = source_settings.ATLAS_SCRAPER_CALLBACK_HMAC_SECRET
    if not secret:
        # Signing disabled — accept all (legacy single-tenant behavior).
        return True
    if not signature:
        return False
    expected = compute_signature(run_id, raw_products, secret)
    return hmac.compare_digest(expected, signature)


@shared_task(name="django_atlas.process_scraper_results", acks_late=True, queue=QUEUE_RESULTS)
def process_scraper_results_task(run_id: str, raw_products: list[dict], signature: str | None = None) -> dict:
    """Validate raw payload, finalize ImportLog, emit events on errors.

    Returns dict {run_id, status, success_count, error_count} for observability.
    """
    if not _verify_signature(run_id, raw_products or [], signature):
        event_service.record(
            event_type=EventType.SCRAPER_CALLBACK_SIGNATURE_INVALID.value,
            severity=EventSeverity.WARNING.value,
            message=f"Scraper callback signature invalid for run_id={run_id}",
            details={"run_id": run_id, "had_signature": bool(signature)},
        )
        return {"run_id": run_id, "status": "error", "success_count": 0, "error_count": 0}

    try:
        run_uuid = UUID(run_id)
    except (TypeError, ValueError):
        event_service.record(
            event_type=EventType.SCRAPER_UNKNOWN_RUN_ID.value,
            severity=EventSeverity.WARNING.value,
            message=f"Invalid run_id payload: {run_id!r}",
            details={"run_id": run_id},
        )
        return {"run_id": run_id, "status": "error", "success_count": 0, "error_count": 0}

    try:
        log = ImportLog.objects.select_related("feed", "feed__source").get(run_id=run_uuid)
    except ImportLog.DoesNotExist:
        event_service.record(
            event_type=EventType.SCRAPER_UNKNOWN_RUN_ID.value,
            severity=EventSeverity.WARNING.value,
            message=f"Scraper callback received for unknown run_id={run_id}",
            details={"run_id": run_id},
        )
        return {"run_id": run_id, "status": "error", "success_count": 0, "error_count": 0}

    if log.status in (LogStatus.SUCCESS.value, LogStatus.PARTIAL.value, LogStatus.FAILED.value):
        # Idempotent: callback retry for an already-finalized ImportLog (re-delivery).
        event_service.record(
            event_type=EventType.SCRAPER_UNKNOWN_RUN_ID.value,
            severity=EventSeverity.WARNING.value,
            feed=log.feed,
            message=f"Scraper callback for already-finalized ImportLog run_id={run_id}, status={log.status}",
            details={"run_id": run_id, "current_status": log.status},
        )
        return {
            "run_id": run_id,
            "status": log.status,
            "success_count": log.new_count + log.updated_count,
            "error_count": log.error_count,
        }

    validated: list[RawProduct] = []
    errors: list[dict] = []
    for index, item in enumerate(raw_products or []):
        try:
            validated.append(RawProduct.model_validate(item))
        except ValidationError as exc:
            # Sanitize: drop raw exception messages (validator names, internals leak),
            # keep only structured (loc, type) for operator-facing event log.
            sanitized = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
            errors.append(
                {
                    "index": index,
                    "external_id": item.get("external_id") if isinstance(item, dict) else None,
                    "error_count": len(sanitized),
                    "fields": sanitized,
                }
            )

    if errors:
        event_service.record(
            event_type=EventType.SCRAPER_RESULTS_VALIDATION_ERRORS.value,
            severity=EventSeverity.WARNING.value,
            feed=log.feed,
            message=f"{len(errors)} of {len(raw_products or [])} raw products failed validation in run_id={run_id}",
            details={"run_id": run_id, "count": len(errors), "sample": errors[:5]},
        )

    if not validated and errors:
        # All invalid: finalize as 'partial' with error_count=len(errors).
        finalized = log_service.finalize_import_log(log.run_id, status=LogStatus.PARTIAL.value, error_count=len(errors))
        return {"run_id": run_id, "status": finalized.status, "success_count": 0, "error_count": len(errors)}

    finalized = import_service.process_feed_results(run_uuid, validated)
    if errors and finalized.status == LogStatus.SUCCESS.value:
        finalized.status = LogStatus.PARTIAL.value
        finalized.error_count += len(errors)
        finalized.save(update_fields=["status", "error_count", "modified_at"])
    return {
        "run_id": run_id,
        "status": finalized.status,
        "success_count": finalized.new_count + finalized.updated_count,
        "error_count": finalized.error_count,
    }
