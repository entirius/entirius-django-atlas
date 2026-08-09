# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

from django_atlas.models import IntegrationEvent
from django_atlas.services import source_service
from tests.factories import CurrencyFactory, LanguageFactory, SourceFactory


@pytest.mark.django_db
def test_list_sources_empty():
    assert list(source_service.list_sources()) == []


@pytest.mark.django_db
def test_list_sources_returns_all():
    SourceFactory.create_batch(3)
    assert source_service.list_sources().count() == 3


@pytest.mark.django_db
def test_list_sources_filters_by_kind():
    SourceFactory(idx="procurement-1")
    qs = source_service.list_sources(kind="procurement")
    assert qs.count() == 1
    assert qs.first().kind == "procurement"


@pytest.mark.django_db
def test_list_sources_filters_by_is_active():
    SourceFactory(idx="active-1", is_active=True)
    SourceFactory(idx="inactive-1", is_active=False)
    qs = source_service.list_sources(is_active=False)
    assert qs.count() == 1
    assert qs.first().idx == "inactive-1"


@pytest.mark.django_db
def test_list_sources_search_matches_idx_or_name():
    SourceFactory(idx="amazon-de", name="Amazon Deutschland")
    SourceFactory(idx="ikea-pl", name="IKEA Polska")
    matches = source_service.list_sources(search="amazon")
    assert matches.count() == 1
    assert matches.first().idx == "amazon-de"


@pytest.mark.django_db
def test_get_source_returns_existing():
    SourceFactory(idx="exists")
    assert source_service.get_source("exists").idx == "exists"


@pytest.mark.django_db
def test_get_source_missing_raises():
    """C4: get_source raises ValueError (translated from DoesNotExist) for view layer."""
    with pytest.raises(ValueError, match="not found"):
        source_service.get_source("missing")


@pytest.mark.django_db
def test_create_source_creates_new():
    language = LanguageFactory()
    currency = CurrencyFactory()
    source = source_service.create_source(
        idx="new-source", name="New Source", default_language=language, default_currency=currency
    )
    assert source.idx == "new-source"


@pytest.mark.django_db
def test_create_source_duplicate_raises_value_error():
    SourceFactory(idx="dup")
    with pytest.raises(ValueError):
        source_service.create_source(
            idx="dup", name="x", default_language=LanguageFactory(), default_currency=CurrencyFactory()
        )


@pytest.mark.django_db
def test_create_source_unsupported_kind_raises_value_error():
    with pytest.raises(ValueError):
        source_service.create_source(
            idx="bogus-kind",
            name="x",
            kind="bogus",
            default_language=LanguageFactory(),
            default_currency=CurrencyFactory(),
        )


@pytest.mark.django_db
def test_update_source_changes_name():
    SourceFactory(idx="upd")
    updated = source_service.update_source("upd", name="New Name")
    assert updated.name == "New Name"


@pytest.mark.django_db
def test_delete_source_soft_is_idempotent():
    SourceFactory(idx="soft")
    first = source_service.delete_source("soft", force=False)
    assert first == {"mode": "soft", "source_idx": "soft"}
    assert source_service.get_source("soft").is_active is False
    second = source_service.delete_source("soft", force=False)
    assert second == {"mode": "soft", "source_idx": "soft"}


@pytest.mark.django_db
def test_delete_source_force_emits_event_and_removes_source():
    SourceFactory(idx="hard-1")
    result = source_service.delete_source("hard-1", force=True)
    assert result["mode"] == "hard"
    assert result["affected_links_count"] == 0
    assert result["affected_pushed_skus_count"] == 0
    with pytest.raises(ValueError, match="not found"):
        source_service.get_source("hard-1")
    events = IntegrationEvent.objects.filter(event_type="source_deleted")
    assert events.count() == 1
    event = events.first()
    assert event.severity == "warning"
    assert event.details["source_idx"] == "hard-1"
    assert event.details["affected_links_count"] == 0
    assert event.details["affected_pushed_skus"] == []


@pytest.mark.django_db
def test_delete_source_force_missing_raises():
    with pytest.raises(ValueError, match="not found"):
        source_service.delete_source("missing", force=True)


@pytest.mark.django_db
def test_update_source_rejects_unknown_field():
    """Mass-assignment defense: only whitelisted fields are editable."""
    SourceFactory(idx="ms-1")
    with pytest.raises(ValueError, match="not editable"):
        source_service.update_source("ms-1", created_at="2020-01-01")
    with pytest.raises(ValueError, match="not editable"):
        source_service.update_source("ms-1", id=999)
