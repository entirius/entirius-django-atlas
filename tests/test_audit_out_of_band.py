# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Out-of-band logging — audit failure NEVER crashes the main data path."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from django_atlas.connectors.base import SyncConnector
from django_atlas.enums import ProductStatus
from django_atlas.models import SourceProduct, SourceProductChangeLog
from django_atlas.schemas.contract import PriceStockUpdate
from django_atlas.services import import_service, log_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def test_delta_sync_completes_when_audit_service_raises(caplog):
    """If audit_service.log_changes_bulk explodes, the sync MUST still update SP rows."""
    source = SourceFactory()
    feed = FeedFactory(source=source, sync_mode="delta")
    SourceProductFactory(
        source=source, feed=feed, external_id="SKU-OOB", cost=Decimal("10.00"), status=ProductStatus.PUSHED.value
    )
    upd = PriceStockUpdate(external_id="SKU-OOB", cost=Decimal("20.00"))
    connector = MagicMock(spec=SyncConnector)
    connector.is_async = False
    connector.fetch_delta.return_value = iter([upd])
    log = log_service.start_import_log(feed, mode="delta")

    with patch(
        "django_atlas.services.import_service.audit_service.log_changes_bulk",
        side_effect=RuntimeError("simulated DB failure"),
    ):
        import_service.process_delta_sync(feed, connector, log)

    # Main path MUST complete: SP cost updated, no audit row written.
    sp = SourceProduct.objects.get(source=source, external_id="SKU-OOB")
    assert sp.cost == Decimal("20.00")
    assert SourceProductChangeLog.objects.count() == 0
    assert any("audit_service.log_changes_bulk failed" in r.message for r in caplog.records)
