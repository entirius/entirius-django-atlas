# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import timedelta
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet
from django.utils import timezone

from django_atlas.enums import EventSeverity, EventType
from django_atlas.models import IntegrationEvent, Source, SourceFeed, SourceProduct

_VALID_EVENT_TYPES = {choice[0] for choice in EventType.choices}
_VALID_SEVERITIES = {choice[0] for choice in EventSeverity.choices}


def _validate_event_kwargs(event_type: str, severity: str, message: str | None) -> None:
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(f"event_type '{event_type}' is not in whitelist (enums.EventType).")
    if severity not in _VALID_SEVERITIES:
        raise ValueError(f"severity '{severity}' is not valid.")
    if not message:
        raise ValueError("message is required.")


def record(
    *,
    event_type: str,
    severity: str,
    message: str,
    source: Source | None = None,
    feed: SourceFeed | None = None,
    source_product: SourceProduct | None = None,
    details: dict | None = None,
) -> IntegrationEvent:
    _validate_event_kwargs(event_type, severity, message)
    return IntegrationEvent.objects.create(
        event_type=event_type,
        severity=severity,
        source=source,
        feed=feed,
        source_product=source_product,
        message=message,
        details=details if details is not None else {},
    )


def record_bulk(events: list[dict[str, Any]]) -> int:
    instances: list[IntegrationEvent] = []
    for index, payload in enumerate(events):
        try:
            event_type = payload["event_type"]
            severity = payload["severity"]
            message = payload["message"]
        except KeyError as exc:
            raise ValueError(f"events[{index}] missing required key: {exc.args[0]}") from exc
        try:
            _validate_event_kwargs(event_type, severity, message)
        except ValueError as exc:
            raise ValueError(f"events[{index}] invalid: {exc}") from exc
        instances.append(
            IntegrationEvent(
                event_type=event_type,
                severity=severity,
                source=payload.get("source"),
                feed=payload.get("feed"),
                source_product=payload.get("source_product"),
                message=message,
                details=payload.get("details", {}),
            )
        )
    IntegrationEvent.objects.bulk_create(instances, batch_size=500)
    return len(instances)


def list_events(
    *,
    severity: str | None = None,
    event_type: str | None = None,
    source_id: int | None = None,
    acknowledged: bool | None = None,
    search: str | None = None,
) -> QuerySet[IntegrationEvent]:
    qs = IntegrationEvent.objects.all()
    if severity is not None:
        qs = qs.filter(severity=severity)
    if event_type is not None:
        qs = qs.filter(event_type=event_type)
    if source_id is not None:
        qs = qs.filter(source_id=source_id)
    if acknowledged is True:
        qs = qs.filter(acknowledged_at__isnull=False)
    elif acknowledged is False:
        qs = qs.filter(acknowledged_at__isnull=True)
    if search:
        qs = qs.filter(message__icontains=search)
    return qs


def get_event(pk: int) -> IntegrationEvent:
    try:
        return IntegrationEvent.objects.get(pk=pk)
    except IntegrationEvent.DoesNotExist as exc:
        raise ValueError(f"IntegrationEvent with pk={pk} not found") from exc


def acknowledge(event_id: int, user: AbstractBaseUser | None) -> IntegrationEvent:
    try:
        event = IntegrationEvent.objects.get(pk=event_id)
    except IntegrationEvent.DoesNotExist as exc:
        raise ValueError(f"IntegrationEvent with pk={event_id} not found") from exc
    if event.acknowledged_at is not None:
        return event
    event.acknowledged_at = timezone.now()
    event.acknowledged_by = user
    event.save(update_fields=["acknowledged_at", "acknowledged_by", "modified_at"])
    return event


def prune_older_than(days: int) -> int:
    if days < 0:
        raise ValueError("days must be >= 0.")
    cutoff = timezone.now() - timedelta(days=days)
    qs = IntegrationEvent.objects.filter(created_at__lt=cutoff).exclude(severity=EventSeverity.CRITICAL.value)
    deleted, _ = qs.delete()
    return deleted
