# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for audit_log emission from process_full_sync.

PUSHED_STATUSES gate is the critical invariant — pre-push staging churn is noise
to the PIM operator, so audit log fires ONLY for SPs already pushed.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from django_atlas.connectors.base import SyncConnector
from django_atlas.enums import ChangeLogSource, ProductStatus
from django_atlas.models import SourceProductChangeLog
from django_atlas.schemas.contract import RawProduct
from django_atlas.services import import_service, log_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _run_full_sync(feed, raws):
    connector = MagicMock(spec=SyncConnector)
    connector.is_async = False
    connector.fetch.return_value = iter(raws)
    log = log_service.start_import_log(feed, mode="full")
    import_service.process_full_sync(feed, connector, log)


def test_full_sync_logs_diff_for_pushed_sp():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-A",
        name="Old name",
        cost=Decimal("10.00"),
        stock=5,
        data={"color": "red"},
        status=ProductStatus.PUSHED.value,
    )
    _run_full_sync(
        feed,
        [
            RawProduct(
                external_id="SKU-A", name="New name", cost=Decimal("12.00"), stock=8, attributes={"color": "blue"}
            )
        ],
    )
    entries = SourceProductChangeLog.objects.filter(source=ChangeLogSource.FULL_SYNC.value)
    field_paths = {e.field_path for e in entries}
    assert "name" in field_paths
    assert "cost" in field_paths
    assert "stock" in field_paths
    assert "data.color" in field_paths
    name_entry = entries.get(field_path="name")
    assert name_entry.before == "Old name"
    assert name_entry.after == "New name"
    assert name_entry.applied_to_pim is False


def test_full_sync_skips_audit_for_non_pushed_sp():
    """PUSHED_STATUSES gate — pre-push churn is noise."""
    source = SourceFactory()
    feed = FeedFactory(source=source)
    SourceProductFactory(
        source=source, feed=feed, external_id="SKU-B", cost=Decimal("10.00"), status=ProductStatus.NEW.value
    )
    _run_full_sync(feed, [RawProduct(external_id="SKU-B", name="x", cost=Decimal("25.00"))])
    assert SourceProductChangeLog.objects.count() == 0


def test_full_sync_logs_image_urls_whole_list_diff():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    SourceProductFactory(
        source=source, feed=feed, external_id="SKU-C", image_urls=["http://a.jpg"], status=ProductStatus.PUSHED.value
    )
    _run_full_sync(feed, [RawProduct(external_id="SKU-C", name="x", images=["http://a.jpg", "http://b.jpg"])])
    img_entry = SourceProductChangeLog.objects.get(field_path="image_urls")
    assert img_entry.before == ["http://a.jpg"]
    assert img_entry.after == ["http://a.jpg", "http://b.jpg"]
