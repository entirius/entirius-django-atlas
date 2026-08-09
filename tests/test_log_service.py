# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for log_service — ImportLog lifecycle (start, finalize, error_summary cap)."""

import pytest

from django_atlas.enums import LogMode, LogStatus
from django_atlas.models import ImportLog
from django_atlas.services import log_service
from tests.factories import FeedFactory


@pytest.mark.django_db
def test_start_creates_running_log_and_updates_feed():
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.FULL.value, source="api")

    assert log.status == LogStatus.RUNNING.value
    assert log.mode == LogMode.FULL.value
    assert log.source == "api"
    assert log.run_id is not None
    assert log.started_at is not None
    feed.refresh_from_db()
    assert feed.last_run_id == log.run_id
    assert feed.last_sync_status == LogStatus.RUNNING.value


@pytest.mark.django_db
def test_finalize_success_marks_finished_and_updates_feed():
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.FULL.value)
    finalized = log_service.finalize_import_log(
        log.run_id, status=LogStatus.SUCCESS.value, total_count=10, new_count=4, updated_count=3, unchanged_count=3
    )
    assert finalized.status == LogStatus.SUCCESS.value
    assert finalized.finished_at is not None
    assert finalized.total_count == 10
    assert finalized.new_count == 4
    feed.refresh_from_db()
    assert feed.last_sync_status == LogStatus.SUCCESS.value
    assert feed.last_sync_at is not None


@pytest.mark.django_db
def test_finalize_failure_persists_error_summary():
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.DELTA.value)
    finalized = log_service.finalize_import_log(
        log.run_id, status=LogStatus.FAILED.value, error_count=5, error_summary=["err 1", "err 2", "err 3"]
    )
    assert finalized.status == LogStatus.FAILED.value
    assert finalized.error_count == 5
    assert finalized.error_summary == ["err 1", "err 2", "err 3"]


@pytest.mark.django_db
def test_finalize_caps_error_summary_at_50():
    """error_summary cap is the JSONField growth guard — defends against pathological feeds."""
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.FULL.value)
    big = [f"err {i}" for i in range(120)]
    finalized = log_service.finalize_import_log(
        log.run_id, status=LogStatus.PARTIAL.value, error_count=120, error_summary=big
    )
    assert len(finalized.error_summary) == 50
    assert finalized.error_summary[0] == "err 0"
    assert finalized.error_summary[49] == "err 49"


@pytest.mark.django_db
def test_finalize_with_no_error_summary_keeps_default_empty():
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.FULL.value)
    finalized = log_service.finalize_import_log(log.run_id, status=LogStatus.SUCCESS.value)
    assert finalized.error_summary == []


@pytest.mark.django_db
def test_finalize_unknown_run_id_raises():
    import uuid

    with pytest.raises(ImportLog.DoesNotExist):
        log_service.finalize_import_log(uuid.uuid4(), status=LogStatus.SUCCESS.value)


@pytest.mark.django_db
def test_finalize_idempotent_same_run_id():
    """Re-finalizing the same run_id updates status (not strictly idempotent — last write wins)."""
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.FULL.value)
    log_service.finalize_import_log(log.run_id, status=LogStatus.SUCCESS.value, total_count=5)
    refinalized = log_service.finalize_import_log(
        log.run_id, status=LogStatus.PARTIAL.value, total_count=8, error_count=1
    )
    assert refinalized.status == LogStatus.PARTIAL.value
    assert refinalized.total_count == 8
    assert refinalized.error_count == 1


@pytest.mark.django_db
def test_start_log_for_test_mode():
    feed = FeedFactory()
    log = log_service.start_import_log(feed, mode=LogMode.TEST.value, source="cli")
    assert log.mode == LogMode.TEST.value
    assert log.source == "cli"
