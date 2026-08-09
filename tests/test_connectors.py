# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for connectors (xml_feed + scraper) and connector_registry."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from django_atlas.connectors.scraper import ScraperConfig, ScraperConnector
from django_atlas.connectors.xml_feed import XmlFeedConnector
from django_atlas.models import IntegrationEvent, SourceSettings
from django_atlas.schemas.contract import PriceStockUpdate, RawProduct
from django_atlas.services import connector_registry
from tests.factories import FeedFactory, SourceFactory

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "xml"
SAMPLE_FULL = (FIXTURE_DIR / "sample_full.xml").read_bytes()
SAMPLE_DELTA = (FIXTURE_DIR / "sample_delta.xml").read_bytes()


_VALID_XML_CONFIG = {
    "feed_url": "https://example.com/feed.xml",
    "field_mapping": {
        "external_id": "./sku/text()",
        "name": "./name/text()",
        "cost": "./price/text()",
        "currency": "./price/@currency",
        "stock": "./stock/text()",
        "ean": "./ean/text()",
        "url": "./url/text()",
        "description": "./description/text()",
        "manufacturer": "./manufacturer/text()",
        "category": "./category/text()",
    },
}


def _mock_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.status_code = 200
    resp.iter_content = MagicMock(return_value=[content])
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture(autouse=True)
def reset_registry():
    connector_registry.reset_registry_for_tests()
    yield
    connector_registry.reset_registry_for_tests()


@pytest.mark.django_db
def test_discover_connectors_returns_xml_and_scraper():
    registry = connector_registry.discover_connectors()
    assert "xml_feed" in registry
    assert "scraper" in registry


@pytest.mark.django_db
def test_get_connector_returns_xml_instance():
    connector = connector_registry.get_connector("xml_feed")
    assert isinstance(connector, XmlFeedConnector)


@pytest.mark.django_db
def test_get_connector_unknown_raises():
    with pytest.raises(KeyError):
        connector_registry.get_connector("does-not-exist")


@pytest.mark.django_db
def test_validate_feed_config_xml_missing_required_raises():
    with pytest.raises(ValidationError):
        connector_registry.validate_feed_config("xml_feed", {})


@pytest.mark.django_db
def test_validate_feed_config_xml_valid_passes():
    connector_registry.validate_feed_config("xml_feed", _VALID_XML_CONFIG)


@pytest.mark.django_db
def test_xml_fetch_returns_five_raw_products():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_FULL)):
        results = list(XmlFeedConnector().fetch(feed))
    assert len(results) == 5
    assert all(isinstance(r, RawProduct) for r in results)


@pytest.mark.django_db
def test_xml_fetch_first_raw_product_fields():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_FULL)):
        first = next(iter(XmlFeedConnector().fetch(feed)))
    assert first.external_id == "SKU-001"
    assert first.name == "Office Chair Black"
    assert first.cost == Decimal("129.00")
    assert first.currency == "EUR"
    assert first.stock == 42
    assert first.ean == "5901234123457"
    assert first.url == "https://example.com/p/sku-001"
    assert len(first.images) == 2
    assert "description" in first.attributes
    assert "manufacturer" in first.attributes
    assert "category" in first.attributes


@pytest.mark.django_db
def test_xml_fetch_sample_returns_limit():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_FULL)):
        results = XmlFeedConnector().fetch_sample(feed, limit=2)
    assert len(results) == 2


@pytest.mark.django_db
def test_xml_fetch_delta_returns_three_updates():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_DELTA)):
        results = list(XmlFeedConnector().fetch_delta(feed))
    assert len(results) == 3
    assert all(isinstance(u, PriceStockUpdate) for u in results)


@pytest.mark.django_db
def test_xml_fetch_delta_update_types():
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(SAMPLE_DELTA)):
        results = list(XmlFeedConnector().fetch_delta(feed))
    first = results[0]
    assert first.cost is None or isinstance(first.cost, Decimal)
    assert first.stock is None or isinstance(first.stock, int)


@pytest.mark.django_db
def test_xml_raw_product_with_missing_price_returns_none():
    no_price_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?><catalog><product><sku>SKU-X</sku><name>X</name></product></catalog>'
    )
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(no_price_xml)):
        results = list(XmlFeedConnector().fetch(feed))
    assert results[0].cost is None


@pytest.mark.django_db
def test_scraper_dispatch_fetch_with_killswitch_on_calls_send_task():
    feed = FeedFactory(
        connector_kind="scraper", feed_config={"scraper_id": "demo-scr", "catalog_url": "https://example.com/cat"}
    )
    SourceSettings.load()  # ensures pk=1 exists
    with patch("django_atlas.connectors.scraper.current_app.send_task") as send_task:
        send_task.return_value = MagicMock(id="task-abc")
        result = ScraperConnector().dispatch_fetch(feed, run_id=uuid4())
    assert result == "task-abc"
    send_task.assert_called_once()
    _, kwargs = send_task.call_args
    assert kwargs["queue"] == "scraping"
    assert kwargs["kwargs"]["mode"] == "full"


@pytest.mark.django_db
def test_scraper_dispatch_fetch_with_killswitch_off_skips_and_records_event():
    feed = FeedFactory(
        connector_kind="scraper", feed_config={"scraper_id": "demo-scr", "catalog_url": "https://example.com/cat"}
    )
    settings = SourceSettings.load()
    settings.scraper_dispatch_enabled = False
    settings.save()
    with patch("django_atlas.connectors.scraper.current_app.send_task") as send_task:
        result = ScraperConnector().dispatch_fetch(feed, run_id=uuid4())
    assert result is None
    send_task.assert_not_called()
    assert IntegrationEvent.objects.filter(event_type="scraper_dispatch_skipped").exists()


@pytest.mark.django_db
def test_scraper_dispatch_fetch_sample_mode_test_kwargs():
    feed = FeedFactory(
        connector_kind="scraper", feed_config={"scraper_id": "demo-scr", "catalog_url": "https://example.com/cat"}
    )
    SourceSettings.load()
    with patch("django_atlas.connectors.scraper.current_app.send_task") as send_task:
        send_task.return_value = MagicMock(id="task-test")
        ScraperConnector().dispatch_fetch_sample(feed, run_id=uuid4(), limit=5)
    _, kwargs = send_task.call_args
    assert kwargs["kwargs"]["mode"] == "test"
    assert kwargs["kwargs"]["limit"] == 5


@pytest.mark.django_db
def test_scraper_validate_config_empty_raises():
    with pytest.raises(ValidationError):
        ScraperConnector().validate_config({})


@pytest.mark.django_db
def test_scraper_config_defaults():
    cfg = ScraperConfig(scraper_id="demo", catalog_url="https://example.com")
    assert cfg.delay_min == 1
    assert cfg.delay_max == 3
    assert cfg.impersonate == "chrome131"


@pytest.mark.django_db
def test_list_connectors_returns_two_with_keys():
    # Real entry_points metadata is process-global — private deployments may register additional atlas_connectors kinds
    # in this environment. Assert core connectors are present rather than an exact count.
    out = connector_registry.list_connectors()
    kinds = {entry["kind"] for entry in out}
    assert {"xml_feed", "scraper"} <= kinds
    for entry in out:
        assert set(entry.keys()) == {"kind", "is_async", "config_schema"}


@pytest.mark.django_db
def test_connector_is_async_attrs():
    assert XmlFeedConnector.is_async is False
    assert ScraperConnector.is_async is True


@pytest.mark.django_db
def test_discover_connectors_idempotent_cache():
    first = connector_registry.discover_connectors()
    second = connector_registry.discover_connectors()
    assert first is second  # same dict object reused


@pytest.mark.django_db
def test_discover_connectors_rejects_foreign_base_class(monkeypatch):
    """Gotcha #8: registry validates issubclass against ITS OWN BaseConnector.

    A connector inheriting from an unrelated base (e.g. a leftover django_suppliers
    connector, or any class that isn't django_atlas.connectors.base.BaseConnector)
    must be rejected at discovery time, not silently registered.
    """

    class _ForeignBase:
        connector_kind = "foreign"

    class _FakeEntryPoint:
        name = "foreign"

        def load(self):
            return _ForeignBase

    monkeypatch.setattr(connector_registry.metadata, "entry_points", lambda group=None: [_FakeEntryPoint()])
    with pytest.raises(TypeError, match="did not resolve to BaseConnector subclass"):
        connector_registry.discover_connectors()


@pytest.mark.django_db
def test_xml_fetch_malformed_raises():
    from lxml.etree import XMLSyntaxError

    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    bad = b"<not-valid-xml"
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(bad)):
        with pytest.raises(XMLSyntaxError):
            list(XmlFeedConnector().fetch(feed))


# Security: XXE / billion-laughs / SSRF (C2, C3) ------------------------


@pytest.mark.django_db
def test_xml_xxe_external_entity_not_resolved():
    """Hardened parser must NOT resolve external entities — file:// payload stays as raw token."""
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    xxe = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<root><product><sku>X1</sku><name>&xxe;</name>"
        b"<price currency='EUR'>10</price></product></root>"
    )
    # The hardened parser raises rather than resolving — both outcomes are safe (no /etc/passwd leak).
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(xxe)):
        try:
            results = list(XmlFeedConnector().fetch(feed))
        except Exception as exc:  # noqa: BLE001 — any parser error means entity wasn't resolved
            assert "passwd" not in str(exc)
            return
    # If no exception: name field must NOT contain /etc/passwd contents (entity left as text/None).
    for r in results:
        assert "root:" not in (r.name or "")


@pytest.mark.django_db
def test_xml_blocks_internal_feed_url(monkeypatch):
    monkeypatch.setattr("django_atlas.security.url_guard.settings.ATLAS_BLOCK_PRIVATE_HOSTS", True, raising=False)
    feed = FeedFactory(feed_config={**_VALID_XML_CONFIG, "feed_url": "http://127.0.0.1/feed.xml"})
    with pytest.raises(ValueError, match="Internal host blocked"):
        list(XmlFeedConnector().fetch(feed))


@pytest.mark.django_db
def test_xml_feed_body_cap_enforced(monkeypatch):
    monkeypatch.setattr("django_atlas.connectors.xml_feed.source_settings.ATLAS_MAX_FEED_BYTES", 100)
    feed = FeedFactory(feed_config=_VALID_XML_CONFIG)
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(b"X" * 200)):
        with pytest.raises(ValueError, match="exceeds cap"):
            list(XmlFeedConnector().fetch(feed))


# Reference SourceFactory to silence unused-import lint while keeping fixtures graph valid
_ = SourceFactory
