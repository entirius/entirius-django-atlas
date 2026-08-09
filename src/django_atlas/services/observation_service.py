# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Read/write service for the append-only Observation log.

`record_observation` is the single write choke point (append-only — no
update/upsert path here or anywhere else in this module). The feed
pipeline will call into it per-row once wired. The retention beat
(`tasks/observation_retention.py`) is the only bulk-delete path.

FK gotcha: every `order_by()` / `distinct()` below uses `source_id`, not
`source` — referencing the FK by name instead of `_id` makes Django add an
implicit join, which silently breaks Postgres `DISTINCT ON`.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from django_atlas.enums import SourceKind
from django_atlas.models import Observation, Source
from django_atlas.signals.definitions import observation_recorded_signal

_PRICE_TYPES = (str, int, float, Decimal)


def _validate_value_shape(kind: str, value: dict[str, Any]) -> None:
    """Enforce the canonical `value` shape per kind (see AGENTS.md § Observation log).

    monitoring -> requires a `price`; enrichment -> requires a `signals` dict. procurement
    sources do not currently write Observations and have no canonical shape defined yet, so
    they are not constrained here.
    """
    if not isinstance(value, dict):
        raise ValueError("value must be a dict.")
    if kind == SourceKind.MONITORING.value:
        price = value.get("price")
        if price is None or isinstance(price, bool) or not isinstance(price, _PRICE_TYPES):
            raise ValueError("monitoring observations require a 'price' key of type str/int/float/Decimal.")
    elif kind == SourceKind.ENRICHMENT.value:
        if not isinstance(value.get("signals"), dict):
            raise ValueError("enrichment observations require a 'signals' dict.")


def record_observation(
    *, source: Source, sku: str, value: dict[str, Any], raw: dict[str, Any] | None = None, ts: datetime | None = None
) -> Observation:
    """Create an Observation row and emit `observation_recorded_signal`.

    `kind` is denormalized from `source.kind` at write time. `ts` defaults to
    now but accepts a caller-supplied value for batch/backfill writes.
    """
    if not sku:
        raise ValueError("sku is required.")
    _validate_value_shape(source.kind, value)
    observation = Observation.objects.create(
        source=source, sku=sku, kind=source.kind, value=value, raw=raw, ts=ts or timezone.now()
    )
    observation_recorded_signal.send(sender=Observation, source=source, sku=sku, kind=source.kind, value=value)
    return observation


def _serialize(observation: Observation) -> dict[str, Any]:
    return {"source": observation.source, "value": observation.value, "ts": observation.ts}


def get_observations(sku: str, kind: str, *, latest_per_source: bool = True) -> list[dict[str, Any]]:
    """Return observations for a single sku+kind. Empty log -> `[]` (never raises).

    `latest_per_source=True` (default, "widok"): newest row per distinct
    source. `latest_per_source=False`: full timeline, newest first.
    """
    if not latest_per_source:
        qs = Observation.objects.filter(sku=sku, kind=kind).select_related("source").order_by("-ts")
        return [_serialize(o) for o in qs]
    qs = (
        Observation.objects.filter(sku=sku, kind=kind)
        .select_related("source")
        .order_by("source_id", "-ts")
        .distinct("source_id")
    )
    return [_serialize(o) for o in sorted(qs, key=lambda o: o.ts, reverse=True)]


def get_skus_with_valid_observations(kind: str, max_age: timedelta) -> set[str]:
    """SKUs with at least one Observation of `kind` newer than `max_age`.

    Single query (`values_list(..., flat=True).distinct()`) — the N+1-safe
    batch entry point pricefighter's engine uses to select candidate skus.
    """
    cutoff = timezone.now() - max_age
    return set(Observation.objects.filter(kind=kind, ts__gte=cutoff).values_list("sku", flat=True).distinct())


def get_observations_bulk(
    skus: list[str], kind: str, *, latest_per_source: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """Batch variant of `get_observations` for many skus in one query.

    Skus with no observations are absent from the returned dict — never
    present as an empty-list key. This mirrors `get_observations`'s "nothing
    found" contract (empty list, never `None`/a sentinel) at the single-sku
    level: "nothing" is represented by omission here, by `[]` there.
    """
    if not skus:
        return {}
    if not latest_per_source:
        qs = Observation.objects.filter(sku__in=skus, kind=kind).select_related("source").order_by("sku", "-ts")
        result: dict[str, list[dict[str, Any]]] = {}
        for observation in qs:
            result.setdefault(observation.sku, []).append(_serialize(observation))
        return result

    qs = (
        Observation.objects.filter(sku__in=skus, kind=kind)
        .select_related("source")
        .order_by("sku", "kind", "source_id", "-ts")
        .distinct("sku", "kind", "source_id")
    )
    result = {}
    for observation in qs:
        result.setdefault(observation.sku, []).append(_serialize(observation))
    for sku_observations in result.values():
        sku_observations.sort(key=lambda entry: entry["ts"], reverse=True)
    return result


def list_observations_queryset(
    *, sku: str | None = None, kind: str | None = None, source_idx: str | None = None, latest_per_source: bool = False
) -> QuerySet[Observation]:
    """Admin-browse queryset (paginated API), distinct from the `get_observations*` read API.

    `latest_per_source=True` collapses to the newest row per (sku, kind, source) via
    Postgres `DISTINCT ON` — same FK gotcha as elsewhere: order by `source_id`, not `source`.
    """
    qs = Observation.objects.select_related("source").all()
    if sku:
        qs = qs.filter(sku=sku)
    if kind:
        qs = qs.filter(kind=kind)
    if source_idx:
        qs = qs.filter(source__idx=source_idx)
    if not latest_per_source:
        return qs.order_by("-ts")
    return qs.order_by("sku", "kind", "source_id", "-ts").distinct("sku", "kind", "source_id")


def prune_older_than(kind: str, days: int) -> int:
    """Delete Observation rows of `kind` older than `days` (by `ts`). Returns deleted row count."""
    if days < 0:
        raise ValueError("days must be >= 0.")
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Observation.objects.filter(kind=kind, ts__lt=cutoff).delete()
    return deleted
