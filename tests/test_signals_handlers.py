# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stage 6: auto-push signal handler.

Tests cover review_mode gating, killswitch (auto_push_enabled, suppress_source_signals),
mode='test' bypass, dispatch_uid registration, and decision #32 (transaction.on_commit
must NOT fire after a rollback).
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import TestCase

from django_atlas.enums import LogStatus
from django_atlas.models import ImportLog, Source, SourceFeed, SourceSettings
from django_atlas.signals.definitions import source_products_imported_signal
from django_atlas.signals.handlers import on_source_products_imported
from django_atlas.signals.killswitch import suppress_source_signals


@pytest.fixture
def auto_source(db, language, currency):
    return Source.objects.create(
        idx="auto-sup", name="Auto", default_language=language, default_currency=currency, review_mode="auto"
    )


@pytest.fixture
def manual_source(db, language, currency):
    return Source.objects.create(
        idx="manual-sup", name="Manual", default_language=language, default_currency=currency, review_mode="manual"
    )


def _make_log(source, *, mode: str = "full") -> tuple[SourceFeed, ImportLog]:
    feed = SourceFeed.objects.create(
        source=source, idx=f"f-{uuid4().hex[:6]}", connector_kind="xml_feed", feed_config={}
    )
    log = ImportLog.objects.create(feed=feed, run_id=uuid4(), mode=mode, source="api", status=LogStatus.SUCCESS.value)
    return feed, log


@pytest.mark.django_db
def test_review_mode_auto_dispatches_after_commit(auto_source):
    feed, log = _make_log(auto_source)
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            on_source_products_imported(sender=None, feed=feed, import_log=log)
        assert mock_delay.call_count == 1
        mock_delay.assert_called_with(source_id=auto_source.id, user_id=None)


@pytest.mark.django_db
def test_review_mode_manual_does_not_dispatch(manual_source):
    feed, log = _make_log(manual_source)
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            on_source_products_imported(sender=None, feed=feed, import_log=log)
        assert mock_delay.call_count == 0


@pytest.mark.django_db
def test_killswitch_auto_push_disabled(auto_source):
    s = SourceSettings.load()
    s.auto_push_enabled = False
    s.save()
    from django_atlas.signals import killswitch as ks

    ks.invalidate_auto_push_cache()
    feed, log = _make_log(auto_source)
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            on_source_products_imported(sender=None, feed=feed, import_log=log)
        assert mock_delay.call_count == 0


@pytest.mark.django_db
def test_suppression_context_skips_handler(auto_source):
    feed, log = _make_log(auto_source)
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            with suppress_source_signals():
                on_source_products_imported(sender=None, feed=feed, import_log=log)
        assert mock_delay.call_count == 0


@pytest.mark.django_db
def test_mode_test_does_not_dispatch(auto_source):
    feed, log = _make_log(auto_source, mode="test")
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        with TestCase.captureOnCommitCallbacks(execute=True):
            on_source_products_imported(sender=None, feed=feed, import_log=log)
        assert mock_delay.call_count == 0


@pytest.mark.django_db
def test_signal_handler_connected_with_dispatch_uid(db):
    uids = {r[0][0] for r in source_products_imported_signal.receivers}
    assert "django_atlas.auto_push" in uids


@pytest.mark.django_db
def test_on_commit_does_not_fire_on_rollback(auto_source):
    """Decision #32: rollback discards on_commit callbacks → task.delay() NOT invoked.

    Pattern: captureOnCommitCallbacks(execute=True) + transaction.atomic() that raises;
    rollback discards the registered callbacks before they would have fired.
    """
    from django.db import transaction

    feed, log = _make_log(auto_source)
    with patch("django_atlas.tasks.push_pipeline.push_approved_for_source_task.delay") as mock_delay:
        try:
            with TestCase.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    on_source_products_imported(sender=None, feed=feed, import_log=log)
                    raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        assert mock_delay.call_count == 0
