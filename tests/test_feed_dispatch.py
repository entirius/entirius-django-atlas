# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the `dispatch_scheduled_feeds` beat task.

`SourceFeed.schedule_cron` had no consumer before this task. Coverage: cron-minute
matching, the global `feed_scheduling_enabled` killswitch, per-feed/per-source
`is_active`, and invalid-cron resilience (skip + warn, never crash the beat).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from django_atlas.models import SourceSettings
from django_atlas.tasks import feed_dispatch
from tests.factories import FeedFactory, SourceFactory

pytestmark = pytest.mark.django_db

_FIXED_NOW = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)  # Thursday


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    monkeypatch.setattr(feed_dispatch.timezone, "now", lambda: _FIXED_NOW)


@pytest.fixture
def dispatch(monkeypatch):
    with patch.object(feed_dispatch.execute_feed_task, "delay") as delay_mock:
        yield delay_mock


def test_dispatches_feed_matching_current_minute(dispatch):
    source = SourceFactory()
    feed = FeedFactory(source=source, schedule_cron="30 10 * * *", sync_mode="full")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 1}
    dispatch.assert_called_once_with(feed.id, mode="full")


def test_skips_feed_not_matching_current_minute(dispatch):
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="31 10 * * *")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()


def test_passes_feed_sync_mode_to_execute_feed_task(dispatch):
    source = SourceFactory()
    feed = FeedFactory(source=source, schedule_cron="30 10 * * *", sync_mode="delta")
    feed_dispatch.dispatch_scheduled_feeds_task.run()
    dispatch.assert_called_once_with(feed.id, mode="delta")


def test_skips_feed_with_empty_schedule_cron(dispatch):
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()


def test_skips_inactive_feed(dispatch):
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="30 10 * * *", is_active=False)
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()


def test_skips_feed_on_inactive_source(dispatch):
    source = SourceFactory(is_active=False)
    FeedFactory(source=source, schedule_cron="30 10 * * *")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()


def test_skips_invalid_cron_without_crashing(dispatch, caplog):
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="not a cron")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()
    assert "Invalid schedule_cron" in caplog.text


def test_skips_cron_with_malformed_field_without_crashing(dispatch, caplog):
    """5 fields but a malformed value (e.g. "-5") raises celery's ParseException,
    not ValueError -- must be caught alongside the field-count ValueError or one bad
    admin-entered cron takes down dispatch for every other feed on this beat tick."""
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="-5 10 * * *")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0}
    dispatch.assert_not_called()
    assert "Invalid schedule_cron" in caplog.text


def test_global_killswitch_disables_dispatch_entirely(dispatch):
    source = SourceFactory()
    FeedFactory(source=source, schedule_cron="30 10 * * *")
    settings = SourceSettings.load()
    settings.feed_scheduling_enabled = False
    settings.save()
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 0, "skipped_reason": "feed_scheduling_disabled"}
    dispatch.assert_not_called()


def test_dispatches_multiple_matching_feeds(dispatch):
    source = SourceFactory()
    FeedFactory(source=source, idx="feed-a", schedule_cron="30 10 * * *")
    FeedFactory(source=source, idx="feed-b", schedule_cron="*/5 * * * *")  # 30 % 5 == 0 -> matches
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 2}
    assert dispatch.call_count == 2


def test_dispatches_feeds_across_multiple_sources(dispatch):
    source_a = SourceFactory()
    source_b = SourceFactory()
    FeedFactory(source=source_a, schedule_cron="30 10 * * *")
    FeedFactory(source=source_b, schedule_cron="30 10 * * *")
    result = feed_dispatch.dispatch_scheduled_feeds_task.run()
    assert result == {"dispatched": 2}
