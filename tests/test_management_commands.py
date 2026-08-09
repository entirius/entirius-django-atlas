# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stage 6: management commands.

call_command-based smoke tests covering happy paths, error handling, and async dispatch.
"""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command

from django_atlas.models import IntegrationEvent, Source, SourceFeed, SourceProduct


@pytest.fixture
def source(db, language, currency):
    return Source.objects.create(idx="cli-sup", name="CLI", default_language=language, default_currency=currency)


@pytest.fixture
def feed(db, source):
    return SourceFeed.objects.create(
        source=source,
        idx="cli-feed",
        connector_kind="xml_feed",
        feed_config={
            "feed_url": "https://x/y.xml",
            "field_mapping": {"external_id": ".//id", "name": ".//name", "cost": ".//cost"},
        },
    )


@pytest.mark.django_db
def test_execute_source_feed_by_id(monkeypatch, feed):
    class _Log:
        run_id = "abc"
        status = "success"
        new_count = 1
        updated_count = 0
        delisted_count = 0
        error_count = 0

    monkeypatch.setattr(
        "django_atlas.services.import_service.execute_feed", lambda f, *, mode, source, triggered_by: _Log()
    )
    out = StringIO()
    call_command("execute_source_feed", f"--feed-id={feed.id}", stdout=out)
    assert "abc" in out.getvalue()


@pytest.mark.django_db
def test_execute_source_feed_by_idx(monkeypatch, source, feed):
    class _Log:
        run_id = "abc"
        status = "success"
        new_count = 0
        updated_count = 0
        delisted_count = 0
        error_count = 0

    monkeypatch.setattr(
        "django_atlas.services.import_service.execute_feed", lambda f, *, mode, source, triggered_by: _Log()
    )
    out = StringIO()
    call_command("execute_source_feed", f"--source-idx={source.idx}", f"--feed-idx={feed.idx}", stdout=out)
    assert "abc" in out.getvalue()


@pytest.mark.django_db
def test_execute_source_feed_unknown_id_raises_command_error(db):
    with pytest.raises(CommandError):
        call_command("execute_source_feed", "--feed-id=999999")


@pytest.mark.django_db
def test_execute_source_feed_async_dispatches(feed):
    with patch("django_atlas.tasks.feed_execution.execute_feed_task.delay") as mock_delay:
        mock_delay.return_value.id = "celery-task-id"
        out = StringIO()
        call_command("execute_source_feed", f"--feed-id={feed.id}", "--async", stdout=out)
        mock_delay.assert_called_once()
        assert "celery-task-id" in out.getvalue()


@pytest.mark.django_db
def test_push_source_approved_happy(monkeypatch, source):
    monkeypatch.setattr(
        "django_atlas.services.push_service.push_approved_for_source",
        lambda *a, **kw: {"success": 3, "failed": 0, "preflight_failed": False, "errors": []},
    )
    out = StringIO()
    call_command("push_source_approved", f"--source-idx={source.idx}", stdout=out)
    assert "success=3" in out.getvalue()


@pytest.mark.django_db
def test_prune_source_events_outputs_count(source):
    out = StringIO()
    call_command("prune_source_events", stdout=out)
    assert "Deleted" in out.getvalue()


@pytest.mark.django_db
def test_source_status_reports_settings_and_counts(source, feed):
    SourceProduct.objects.create(source=source, external_id="X-1", name="X", status="approved")
    IntegrationEvent.objects.create(event_type="push_succeeded", severity="info", source=source, message="ok")
    out = StringIO()
    call_command("source_status", stdout=out)
    body = out.getvalue()
    assert "auto_push_enabled" in body
    assert "Active sources" in body
    assert "Recent Import Logs" in body
    assert "Unack Events" in body


@pytest.mark.django_db
def test_push_source_approved_unknown_source_raises(db):
    with pytest.raises(CommandError):
        call_command("push_source_approved", "--source-idx=missing-source")
