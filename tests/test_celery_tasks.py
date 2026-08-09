# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for tasks package re-exports.

Celery's autodiscover_tasks() walks the configured app list and imports
``django_atlas.tasks``. Without explicit re-exports the submodule
decorators run only when something else (admin view, signal handler, test)
imports them by full path. Workbook §3 Fix #1: tasks/__init__.py re-exports
all 5 @shared_task definitions so the worker sees them under the canonical
``django_atlas.tasks.<name>`` paths.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache

from django_atlas import tasks
from django_atlas.enums import LogStatus
from django_atlas.services import data_inspector_service
from django_atlas.tasks.feed_execution import execute_feed_task

EXPECTED_TASKS = (
    "execute_feed_task",
    "download_source_images_task",
    "push_approved_for_source_task",
    "prune_source_events_task",
    "process_scraper_results_task",
    # auto-primary batch evaluator (celery beat target).
    "evaluate_primary_sources_task",
    # observation-log retention beat.
    "prune_observations_task",
    "prune_import_logs_task",
    # beat dispatcher for schedule_cron.
    "dispatch_scheduled_feeds_task",
)


def test_all_tasks_reexported_on_package():
    """Importing django_atlas.tasks must expose every @shared_task by short name."""
    for name in EXPECTED_TASKS:
        assert hasattr(tasks, name), f"django_atlas.tasks is missing {name!r} re-export"


def test_reexported_tasks_are_callable_celery_tasks():
    """Each re-export must be a real Celery task object (has .delay / .apply_async)."""
    for name in EXPECTED_TASKS:
        task = getattr(tasks, name)
        assert callable(task), f"{name} re-export is not callable"
        assert hasattr(task, "delay"), f"{name} is not a Celery @shared_task (no .delay)"
        assert hasattr(task, "apply_async"), f"{name} is not a Celery @shared_task (no .apply_async)"


def test_all_listed_in_dunder_all():
    """__all__ must enumerate every re-exported task — keeps wildcard imports honest."""
    assert set(tasks.__all__) == set(EXPECTED_TASKS)


@pytest.mark.django_db
def test_execute_feed_task_invalidates_data_keys_on_success(language, currency):
    """a successful feed run must invalidate the data-keys cache for the source.

    Otherwise the CMS combobox keeps showing stale keys until TTL expires.
    """
    from .factories import FeedFactory

    feed = FeedFactory()
    cache.set(data_inspector_service.cache_key(feed.source.idx), {"sentinel": True}, 300)

    fake_log = MagicMock(status=LogStatus.SUCCESS.value, run_id="0" * 36)
    with patch("django_atlas.tasks.feed_execution.import_service.execute_feed", return_value=fake_log):
        execute_feed_task.run(feed.pk)

    assert cache.get(data_inspector_service.cache_key(feed.source.idx)) is None


@pytest.mark.django_db
def test_execute_feed_task_keeps_cache_on_failure(language, currency):
    """If the feed run fails, SourceProduct.data didn't change → cache stays warm."""
    from .factories import FeedFactory

    feed = FeedFactory()
    cache.set(data_inspector_service.cache_key(feed.source.idx), {"sentinel": True}, 300)

    fake_log = MagicMock(status=LogStatus.FAILED.value, run_id="0" * 36)
    with patch("django_atlas.tasks.feed_execution.import_service.execute_feed", return_value=fake_log):
        execute_feed_task.run(feed.pk)

    assert cache.get(data_inspector_service.cache_key(feed.source.idx)) == {"sentinel": True}


@pytest.mark.django_db
def test_execute_feed_task_invalidates_data_values_on_success(language, currency):
    """a successful feed run must drop every cached data-values slot for the source.

    Source values change with each feed pull (new categories appear, old ones drop) — stale
    cache would mislead the operator's picker in CategoryMapping rows.
    """
    from .factories import FeedFactory

    feed = FeedFactory()
    source_idx = feed.source.idx
    # Two cached source_field slots + registry entry (mimics what list_values writes)
    cache.set(data_inspector_service.cache_key_values(source_idx, "category_path"), {"sentinel": "cat"}, 300)
    cache.set(data_inspector_service.cache_key_values(source_idx, "color"), {"sentinel": "col"}, 300)
    cache.set(data_inspector_service.cache_key_values_registry(source_idx), ["category_path", "color"], 600)

    fake_log = MagicMock(status=LogStatus.SUCCESS.value, run_id="0" * 36)
    with patch("django_atlas.tasks.feed_execution.import_service.execute_feed", return_value=fake_log):
        execute_feed_task.run(feed.pk)

    assert cache.get(data_inspector_service.cache_key_values(source_idx, "category_path")) is None
    assert cache.get(data_inspector_service.cache_key_values(source_idx, "color")) is None
    assert cache.get(data_inspector_service.cache_key_values_registry(source_idx)) is None
