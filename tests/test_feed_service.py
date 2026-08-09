# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for feed_service (CRUD + test_feed + trigger_feed_run)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from django_atlas.models import SourceFeed, SourceSettings
from django_atlas.services import feed_service, import_service
from tests.factories import FeedFactory, SourceFactory

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "xml"
SAMPLE_FULL = (FIXTURE_DIR / "sample_full.xml").read_bytes()


_VALID_XML_CONFIG = {
    "feed_url": "https://example.com/feed.xml",
    "field_mapping": {
        "external_id": "./sku/text()",
        "name": "./name/text()",
        "cost": "./price/text()",
        "currency": "./price/@currency",
        "stock": "./stock/text()",
    },
}


def _mock_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.status_code = 200
    resp.iter_content = MagicMock(return_value=[content])
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.django_db
def test_create_feed_with_valid_config():
    source = SourceFactory()
    feed = feed_service.create_feed(source.idx, idx="primary", connector_kind="xml_feed", feed_config=_VALID_XML_CONFIG)
    assert feed.id is not None
    assert feed.connector_kind == "xml_feed"


@pytest.mark.django_db
def test_create_feed_duplicate_idx_per_source_raises():
    source = SourceFactory()
    feed_service.create_feed(source.idx, idx="dup", connector_kind="xml_feed", feed_config=_VALID_XML_CONFIG)
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        feed_service.create_feed(source.idx, idx="dup", connector_kind="xml_feed", feed_config=_VALID_XML_CONFIG)


@pytest.mark.django_db
def test_create_feed_invalid_config_raises():
    source = SourceFactory()
    with pytest.raises(ValidationError):
        feed_service.create_feed(source.idx, idx="bad", connector_kind="xml_feed", feed_config={})


@pytest.mark.django_db
def test_create_feed_invalid_connector_kind_raises():
    source = SourceFactory()
    with pytest.raises(KeyError):
        feed_service.create_feed(
            source.idx, idx="x", connector_kind="ghost", feed_config={"feed_url": "a", "field_mapping": {}}
        )


@pytest.mark.django_db
def test_update_feed_with_changed_feed_config_revalidates():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    new_cfg = dict(_VALID_XML_CONFIG)
    new_cfg["product_xpath"] = ".//item"
    updated = feed_service.update_feed(feed.source.idx, feed.idx, feed_config=new_cfg)
    assert updated.feed_config["product_xpath"] == ".//item"


@pytest.mark.django_db
def test_update_feed_with_invalid_feed_config_raises():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with pytest.raises(ValidationError):
        feed_service.update_feed(feed.source.idx, feed.idx, feed_config={})


@pytest.mark.django_db
def test_update_feed_schedule_cron_persists():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    updated = feed_service.update_feed(feed.source.idx, feed.idx, schedule_cron="0 6 * * *")
    assert updated.schedule_cron == "0 6 * * *"


@pytest.mark.django_db
def test_delete_feed_removes_record():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    feed_service.delete_feed(feed.source.idx, feed.idx)
    assert not SourceFeed.objects.filter(pk=feed.pk).exists()


@pytest.mark.django_db
def test_list_feeds_filters_by_source():
    sup_a = SourceFactory(idx="sup-a")
    sup_b = SourceFactory(idx="sup-b")
    FeedFactory(source=sup_a, feed_config=_VALID_XML_CONFIG)
    FeedFactory(source=sup_b, feed_config=_VALID_XML_CONFIG)
    qs = feed_service.list_feeds("sup-a")
    assert qs.count() == 1
    assert qs.first().source.idx == "sup-a"


@pytest.mark.django_db
def test_get_feed_returns_instance():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    assert feed_service.get_feed(feed.source.idx, feed.idx).pk == feed.pk


@pytest.mark.django_db
def test_get_feed_missing_raises_value_error():
    """C4: service raises ValueError (translated from DoesNotExist) so views catch one type."""
    SourceFactory(idx="empty-source")
    with pytest.raises(ValueError, match="not found"):
        feed_service.get_feed("empty-source", "nonexistent")


@pytest.mark.django_db
def test_test_feed_xml_returns_dump_list():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_FULL)):
        out = feed_service.test_feed(feed.source.idx, feed.idx, limit=10)
    assert isinstance(out, list)
    assert all(isinstance(x, dict) for x in out)
    assert len(out) == 5  # fixture has 5 products, limit 10 → 5 returned


@pytest.mark.django_db
def test_test_feed_scraper_dispatched():
    feed = FeedFactory(connector_kind="scraper", feed_config={"scraper_id": "s", "catalog_url": "https://example.com"})
    SourceSettings.load()
    with patch("django_atlas.connectors.scraper.current_app.send_task") as st:
        st.return_value = MagicMock(id="t-1")
        out = feed_service.test_feed(feed.source.idx, feed.idx, limit=3)
    assert out["status"] == "dispatched"
    assert out["task_id"] == "t-1"


@pytest.mark.django_db
def test_test_feed_scraper_killswitch_off_suppressed():
    feed = FeedFactory(connector_kind="scraper", feed_config={"scraper_id": "s", "catalog_url": "https://example.com"})
    settings = SourceSettings.load()
    settings.scraper_dispatch_enabled = False
    settings.save()
    out = feed_service.test_feed(feed.source.idx, feed.idx, limit=3)
    assert out["status"] == "suppressed"


@pytest.mark.django_db
def test_trigger_feed_run_calls_execute_feed_and_returns_log():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    sentinel = MagicMock(name="ImportLog-sentinel")
    with patch.object(import_service, "execute_feed", return_value=sentinel) as spy:
        result = feed_service.trigger_feed_run(feed.source.idx, feed.idx)
    spy.assert_called_once()
    assert result is sentinel


@pytest.mark.django_db
def test_list_feeds_source_isolation():
    sup_x = SourceFactory(idx="x")
    sup_y = SourceFactory(idx="y")
    FeedFactory(source=sup_x, idx="f1", feed_config=_VALID_XML_CONFIG)
    FeedFactory(source=sup_y, idx="f2", feed_config=_VALID_XML_CONFIG)
    assert feed_service.list_feeds("x").count() == 1
    assert feed_service.list_feeds("y").count() == 1


@pytest.mark.django_db
def test_create_feed_defaults():
    source = SourceFactory()
    feed = feed_service.create_feed(
        source.idx, idx="defaults", connector_kind="xml_feed", feed_config=_VALID_XML_CONFIG
    )
    assert feed.sync_mode == "full"
    assert feed.is_active is True


@pytest.mark.django_db
def test_trigger_feed_run_updates_last_sync_status():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_FULL)):
        feed_service.trigger_feed_run(feed.source.idx, feed.idx)
    feed.refresh_from_db()
    assert feed.last_sync_status in {"success", "partial"}
    assert feed.last_sync_at is not None


@pytest.mark.django_db
def test_update_feed_rejects_unknown_field():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with pytest.raises(ValueError, match="not editable"):
        feed_service.update_feed(feed.source.idx, feed.idx, last_sync_at="2020-01-01")
