# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Any
from uuid import uuid4

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet
from django_regional.models.currency import Currency
from django_regional.models.language import Language

from django_atlas.models import ImportLog, Source, SourceFeed
from django_atlas.services import connector_registry

_EDITABLE_FIELDS = frozenset(
    {
        "connector_kind",
        "feed_config",
        "sync_mode",
        "schedule_cron",
        "feature_set_idx",
        "is_active",
        "language",
        "currency",
    }
)


def list_feeds(source_idx: str) -> QuerySet[SourceFeed]:
    return SourceFeed.objects.filter(source__idx=source_idx)


def get_feed(source_idx: str, feed_idx: str) -> SourceFeed:
    try:
        return SourceFeed.objects.get(source__idx=source_idx, idx=feed_idx)
    except SourceFeed.DoesNotExist as exc:
        raise ValueError(f"Feed '{feed_idx}' not found for source '{source_idx}'") from exc


def source_owns_feeds(source_idx: str) -> bool:
    """Check if source exists (used by feed list endpoint to return 404)."""
    return Source.objects.filter(idx=source_idx).exists()


def _resolve_language(language_id: int | None) -> Language | None:
    if language_id is None:
        return None
    try:
        return Language.objects.get(pk=language_id)
    except Language.DoesNotExist as exc:
        raise ValueError(f"Language with id={language_id} not found") from exc


def _resolve_currency(currency_id: int | None) -> Currency | None:
    if currency_id is None:
        return None
    try:
        return Currency.objects.get(pk=currency_id)
    except Currency.DoesNotExist as exc:
        raise ValueError(f"Currency with id={currency_id} not found") from exc


def create_feed(
    source_idx: str,
    *,
    idx: str,
    connector_kind: str,
    feed_config: dict,
    sync_mode: str = "full",
    schedule_cron: str = "",
    feature_set_idx: str | None = None,
    is_active: bool = True,
    language: Language | None = None,
    currency: Currency | None = None,
    language_id: int | None = None,
    currency_id: int | None = None,
) -> SourceFeed:
    """Create a feed. FK resolution accepts either resolved objects (legacy callers)
    or `_id` ints (primary path from API layer — keeps ORM out of views per C4)."""
    if language is None and language_id is not None:
        language = _resolve_language(language_id)
    if currency is None and currency_id is not None:
        currency = _resolve_currency(currency_id)
    try:
        source = Source.objects.get(idx=source_idx)
    except Source.DoesNotExist as exc:
        raise ValueError(f"Source '{source_idx}' not found") from exc
    connector_registry.validate_feed_config(connector_kind, feed_config)
    return SourceFeed.objects.create(
        source=source,
        idx=idx,
        connector_kind=connector_kind,
        feed_config=feed_config,
        sync_mode=sync_mode,
        schedule_cron=schedule_cron,
        feature_set_idx=feature_set_idx,
        is_active=is_active,
        language=language,
        currency=currency,
    )


def update_feed(source_idx: str, feed_idx: str, **fields: Any) -> SourceFeed:
    # Accept _id ints from API layer, resolve internally (C4: views must not import models).
    if "language_id" in fields:
        fields["language"] = _resolve_language(fields.pop("language_id"))
    if "currency_id" in fields:
        fields["currency"] = _resolve_currency(fields.pop("currency_id"))
    invalid = set(fields) - _EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_feed: {sorted(invalid)}")
    feed = get_feed(source_idx, feed_idx)
    new_kind = fields.get("connector_kind", feed.connector_kind)
    new_config = fields.get("feed_config", feed.feed_config)
    if "feed_config" in fields or "connector_kind" in fields:
        connector_registry.validate_feed_config(new_kind, new_config)
    for key, value in fields.items():
        setattr(feed, key, value)
    feed.save()
    return feed


def delete_feed(source_idx: str, feed_idx: str) -> None:
    feed = get_feed(source_idx, feed_idx)  # raises ValueError when missing
    feed.delete()


def test_feed(source_idx: str, feed_idx: str, *, limit: int = 10) -> dict | list[dict]:
    feed = get_feed(source_idx, feed_idx)
    connector = connector_registry.get_connector(feed.connector_kind)
    if connector.is_async:
        run_id = uuid4()
        task_id = connector.dispatch_fetch_sample(feed, run_id, limit)
        if task_id is None:
            return {"status": "suppressed", "reason": "scraper_dispatch_disabled"}
        return {"status": "dispatched", "task_id": task_id, "run_id": str(run_id)}
    return [r.model_dump(mode="json") for r in connector.fetch_sample(feed, limit)]


def trigger_feed_run(
    source_idx: str, feed_idx: str, *, mode: str = "full", user: AbstractBaseUser | None = None
) -> ImportLog:
    from django_atlas.services import import_service

    feed = get_feed(source_idx, feed_idx)
    return import_service.execute_feed(feed, mode=mode, source="api", triggered_by=user)
