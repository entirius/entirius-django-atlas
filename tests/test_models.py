# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from django_atlas.enums import EventSeverity, EventType, SourceKind
from django_atlas.models import IntegrationEvent, Source, SourceSettings
from tests.factories import CurrencyFactory, LanguageFactory, SourceFactory, SourceProductFactory


@pytest.mark.django_db
def test_source_can_be_created_and_fetched():
    source = SourceFactory(idx="test-1", name="Test One")
    assert Source.objects.get(idx="test-1") == source


@pytest.mark.django_db
def test_source_duplicate_idx_raises_integrity_error():
    SourceFactory(idx="dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        Source.objects.create(
            idx="dup", name="Other", default_language=LanguageFactory(), default_currency=CurrencyFactory()
        )


@pytest.mark.django_db
def test_source_settings_load_returns_pk_one():
    settings = SourceSettings.load()
    assert settings.pk == 1


@pytest.mark.django_db
def test_source_settings_load_is_idempotent():
    a = SourceSettings.load()
    b = SourceSettings.load()
    assert a.pk == b.pk == 1
    assert SourceSettings.objects.count() == 1


@pytest.mark.django_db
def test_source_settings_save_forces_pk_one():
    SourceSettings.load()
    instance = SourceSettings(pk=2, auto_push_enabled=False)
    instance.save()
    assert instance.pk == 1
    assert SourceSettings.objects.count() == 1


@pytest.mark.django_db
def test_source_settings_defaults():
    SourceSettings.objects.all().delete()
    settings = SourceSettings.load()
    assert settings.auto_push_enabled is True
    assert settings.scraper_dispatch_enabled is True
    assert settings.delta_sync_enabled is True
    assert settings.integration_event_retention_days == 90


@pytest.mark.django_db
def test_source_qty_subtract_negative_rejected():
    source = SourceFactory(idx="neg-sub", qty_subtract=-1)
    with pytest.raises(ValidationError):
        source.full_clean()


@pytest.mark.django_db
def test_source_qty_minimum_negative_rejected():
    source = SourceFactory(idx="neg-min", qty_minimum=-1)
    with pytest.raises(ValidationError):
        source.full_clean()


@pytest.mark.django_db
def test_source_defaults():
    source = SourceFactory(idx="defaults")
    assert source.kind == "procurement"
    assert source.source_type == "feed"
    assert source.review_mode == "manual"
    assert source.is_active is True
    assert source.is_trusted is True
    assert source.currency is None


@pytest.mark.django_db
def test_source_product_observed_price_rejected_when_kind_not_monitoring():
    source = SourceFactory(idx="obs-price-procurement", kind=SourceKind.PROCUREMENT.value)
    sp = SourceProductFactory(source=source, observed_price="9.99")
    with pytest.raises(ValidationError):
        sp.full_clean()


@pytest.mark.django_db
def test_source_product_observed_price_accepted_when_kind_monitoring():
    source = SourceFactory(idx="obs-price-monitoring", kind=SourceKind.MONITORING.value)
    sp = SourceProductFactory(source=source, observed_price="9.99", cost=None)
    sp.full_clean()  # must not raise


@pytest.mark.django_db
def test_source_product_cost_rejected_when_kind_not_procurement():
    source = SourceFactory(idx="cost-monitoring", kind=SourceKind.MONITORING.value)
    sp = SourceProductFactory(source=source, cost="100.00")
    with pytest.raises(ValidationError):
        sp.full_clean()


@pytest.mark.django_db
def test_source_product_cost_accepted_when_kind_procurement():
    source = SourceFactory(idx="cost-procurement", kind=SourceKind.PROCUREMENT.value)
    sp = SourceProductFactory(source=source, cost="100.00")
    sp.full_clean()  # must not raise


@pytest.mark.django_db
def test_integration_event_can_be_created_with_minimal_args():
    event = IntegrationEvent.objects.create(
        event_type=EventType.IMPORT_COMPLETED.value, severity=EventSeverity.INFO.value, message="Import completed"
    )
    assert event.pk is not None


@pytest.mark.django_db
def test_integration_event_details_default_is_empty_dict():
    event = IntegrationEvent.objects.create(
        event_type=EventType.IMPORT_COMPLETED.value, severity=EventSeverity.INFO.value, message="x"
    )
    assert event.details == {}


@pytest.mark.django_db
def test_integration_event_str_contains_type_and_severity():
    event = IntegrationEvent.objects.create(
        event_type=EventType.IMPORT_COMPLETED.value, severity=EventSeverity.INFO.value, message="x"
    )
    rendered = str(event)
    assert "import_completed" in rendered
    assert "info" in rendered
