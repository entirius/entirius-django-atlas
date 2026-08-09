# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from django_atlas.models import IntegrationEvent
from django_atlas.services import event_service
from tests.factories import SourceFactory


@pytest.mark.django_db
def test_record_creates_event():
    event = event_service.record(event_type="cost_updated", severity="info", message="test")
    assert event.pk is not None
    assert event.event_type == "cost_updated"


@pytest.mark.django_db
def test_record_unknown_event_type_raises_value_error():
    with pytest.raises(ValueError):
        event_service.record(event_type="not_a_real_event", severity="info", message="x")


@pytest.mark.django_db
def test_record_default_details_is_empty_dict():
    event = event_service.record(event_type="cost_updated", severity="info", message="x", details=None)
    assert event.details == {}


@pytest.mark.django_db
def test_list_events_filters_by_severity():
    event_service.record(event_type="cost_updated", severity="critical", message="c")
    event_service.record(event_type="cost_updated", severity="info", message="i")
    qs = event_service.list_events(severity="critical")
    assert qs.count() == 1


@pytest.mark.django_db
def test_list_events_filters_by_source():
    source = SourceFactory()
    event = event_service.record(event_type="cost_updated", severity="info", message="x", source=source)
    other = event_service.record(event_type="cost_updated", severity="info", message="y")
    matches = list(event_service.list_events(source_id=source.pk))
    assert matches == [event] or matches == [event] + []
    assert other not in matches


@pytest.mark.django_db
def test_list_events_filter_acknowledged_false_returns_unacknowledged():
    event_service.record(event_type="cost_updated", severity="info", message="x")
    unack = event_service.list_events(acknowledged=False)
    assert unack.count() == 1
    assert unack.first().acknowledged_at is None


@pytest.mark.django_db
def test_acknowledge_sets_at_and_by():
    event = event_service.record(event_type="cost_updated", severity="info", message="x")
    user = User.objects.create_user(username="ack-user", password="pass")
    acked = event_service.acknowledge(event.pk, user)
    assert acked.acknowledged_at is not None
    assert acked.acknowledged_by == user


@pytest.mark.django_db
def test_acknowledge_is_idempotent():
    event = event_service.record(event_type="cost_updated", severity="info", message="x")
    user = User.objects.create_user(username="ack2", password="pass")
    first = event_service.acknowledge(event.pk, user)
    timestamp = first.acknowledged_at
    second = event_service.acknowledge(event.pk, user)
    assert second.acknowledged_at == timestamp


@pytest.mark.django_db
def test_prune_older_than_preserves_critical():
    crit = event_service.record(event_type="push_failed", severity="critical", message="c")
    info = event_service.record(event_type="push_succeeded", severity="info", message="i")
    warn = event_service.record(event_type="image_failed", severity="warning", message="w")
    cutoff = timezone.now() + timedelta(seconds=10)
    IntegrationEvent.objects.filter(pk__in=[crit.pk, info.pk, warn.pk]).update(created_at=cutoff - timedelta(days=10))
    deleted = event_service.prune_older_than(0)
    assert deleted == 2
    assert IntegrationEvent.objects.filter(pk=crit.pk).exists()


@pytest.mark.django_db
def test_prune_older_than_negative_raises():
    with pytest.raises(ValueError):
        event_service.prune_older_than(-1)


@pytest.mark.django_db
def test_record_bulk_inserts_in_single_query(django_assert_num_queries):
    payloads = [{"event_type": "cost_updated", "severity": "info", "message": f"msg {i}"} for i in range(5)]
    with django_assert_num_queries(1):
        count = event_service.record_bulk(payloads)
    assert count == 5
    assert IntegrationEvent.objects.count() == 5


@pytest.mark.django_db
def test_record_bulk_invalid_element_raises_with_index():
    payloads = [
        {"event_type": "cost_updated", "severity": "info", "message": "ok-0"},
        {"event_type": "cost_updated", "severity": "info", "message": "ok-1"},
        {"event_type": "not_real", "severity": "info", "message": "bad"},
        {"event_type": "cost_updated", "severity": "info", "message": "ok-3"},
    ]
    with pytest.raises(ValueError, match=r"events\[2\]"):
        event_service.record_bulk(payloads)
    assert IntegrationEvent.objects.count() == 0
