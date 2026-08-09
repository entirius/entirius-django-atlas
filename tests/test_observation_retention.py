# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retention coverage for the observation-log purge beat.

`prune_observations_task` — per-kind retention window (default 180, config
via `SourceSettings.observation_retention_days`), deletes stale rows, keeps
fresh rows, respects independent per-kind cutoffs in the same test DB.

`prune_import_logs_task` — single retention window
(`SourceSettings.import_log_retention_days`, default 90).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from django_atlas.enums import SourceKind
from django_atlas.models import ImportLog, Observation, SourceSettings
from django_atlas.services import log_service, observation_service
from django_atlas.tasks.import_log_retention import prune_import_logs_task
from django_atlas.tasks.observation_retention import prune_observations_task
from tests.factories import FeedFactory, ImportLogFactory, SourceFactory

pytestmark = pytest.mark.django_db


def _make_observation(source, *, days_old: int) -> Observation:
    if source.kind == SourceKind.ENRICHMENT.value:
        value = {"signals": {"note": "retention-fixture"}}
    else:
        value = {"price": "1", "currency": "EUR"}
    observation = observation_service.record_observation(source=source, sku="SKU-1", value=value)
    Observation.objects.filter(pk=observation.pk).update(ts=timezone.now() - timedelta(days=days_old))
    return observation


def test_prune_observations_task_deletes_stale_keeps_fresh_default_180():
    source = SourceFactory(kind=SourceKind.MONITORING.value)
    stale = _make_observation(source, days_old=200)
    fresh = _make_observation(source, days_old=10)

    result = prune_observations_task()

    assert not Observation.objects.filter(pk=stale.pk).exists()
    assert Observation.objects.filter(pk=fresh.pk).exists()
    assert result["deleted_by_kind"][SourceKind.MONITORING.value] == 1


def test_prune_observations_task_respects_independent_per_kind_config():
    monitoring_source = SourceFactory(kind=SourceKind.MONITORING.value)
    enrichment_source = SourceFactory(kind=SourceKind.ENRICHMENT.value)

    settings = SourceSettings.load()
    settings.observation_retention_days = {"monitoring": 30, "enrichment": 200}
    settings.save()

    # 45 days old: stale for monitoring (30d), fresh for enrichment (200d).
    monitoring_old = _make_observation(monitoring_source, days_old=45)
    enrichment_old = _make_observation(enrichment_source, days_old=45)

    result = prune_observations_task()

    assert not Observation.objects.filter(pk=monitoring_old.pk).exists()
    assert Observation.objects.filter(pk=enrichment_old.pk).exists()
    assert result["deleted_by_kind"] == {
        SourceKind.PROCUREMENT.value: 0,
        SourceKind.MONITORING.value: 1,
        SourceKind.ENRICHMENT.value: 0,
    }


def test_prune_observations_task_missing_kind_key_falls_back_to_180():
    source = SourceFactory(kind=SourceKind.ENRICHMENT.value)
    settings = SourceSettings.load()
    settings.observation_retention_days = {"monitoring": 30}  # enrichment key absent
    settings.save()

    stale = _make_observation(source, days_old=200)  # older than 180 fallback
    fresh = _make_observation(source, days_old=100)  # newer than 180 fallback

    prune_observations_task()

    assert not Observation.objects.filter(pk=stale.pk).exists()
    assert Observation.objects.filter(pk=fresh.pk).exists()


def test_observation_service_prune_older_than_rejects_negative_days():
    with pytest.raises(ValueError, match="days must be >= 0"):
        observation_service.prune_older_than(SourceKind.MONITORING.value, -1)


def _make_import_log(*, days_old: int) -> ImportLog:
    log = ImportLogFactory(feed=FeedFactory())
    ImportLog.objects.filter(pk=log.pk).update(started_at=timezone.now() - timedelta(days=days_old))
    return log


def test_prune_import_logs_task_deletes_stale_keeps_fresh_default_90():
    stale = _make_import_log(days_old=120)
    fresh = _make_import_log(days_old=5)

    result = prune_import_logs_task()

    assert not ImportLog.objects.filter(pk=stale.pk).exists()
    assert ImportLog.objects.filter(pk=fresh.pk).exists()
    assert result == {"deleted": 1, "retention_days": 90}


def test_prune_import_logs_task_uses_settings_override():
    settings = SourceSettings.load()
    settings.import_log_retention_days = 10
    settings.save()
    stale = _make_import_log(days_old=15)
    fresh = _make_import_log(days_old=5)

    result = prune_import_logs_task()

    assert not ImportLog.objects.filter(pk=stale.pk).exists()
    assert ImportLog.objects.filter(pk=fresh.pk).exists()
    assert result == {"deleted": 1, "retention_days": 10}


def test_log_service_prune_older_than_rejects_negative_days():
    with pytest.raises(ValueError, match="days must be >= 0"):
        log_service.prune_older_than(-1)
