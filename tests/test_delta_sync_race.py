# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for process_delta_sync counts split + audit/event surface.

Unit-level branch coverage lives in `test_physical_race.py`. These tests exercise the
full delta batch path including the new `physical_*_count` keys and the regression
guard that `updated_count` stays cost/qty-only.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from django_atlas.connectors.base import SyncConnector
from django_atlas.enums import ChangeLogSource, EventType, ProductStatus
from django_atlas.models import IntegrationEvent, SourceProductChangeLog, SourceProductLink
from django_atlas.schemas.contract import PriceStockUpdate
from django_atlas.services import import_service, log_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _run_delta(feed, updates) -> dict:
    connector = MagicMock(spec=SyncConnector)
    connector.is_async = False
    connector.fetch_delta.return_value = iter(updates)
    log = log_service.start_import_log(feed, mode="delta")
    return import_service.process_delta_sync(feed, connector, log)


def _pushed_sp_with_rp(*, source, feed, external_id: str, sku: str, initial_weight: Decimal):
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku=sku, weight=initial_weight)
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id=external_id,
        status=ProductStatus.PUSHED.value,
        cost=Decimal("10.00"),
        currency="EUR",
        stock=5,
    )
    sp.real_product = rp
    sp.save(update_fields=["real_product"])
    return sp, rp


def test_delta_sync_counts_split_physical_outcomes():
    """Two SPs in one delta batch — primary applies, non-primary skips.

    Locks the contract: `updated_count` reflects cost/qty change on the primary SP only;
    `physical_updated_count` and `physical_skipped_non_primary_count` are split.
    """
    primary_source = SourceFactory(idx="delta-race-pref")
    non_pref_source = SourceFactory(idx="delta-race-non")
    feed_pref = FeedFactory(source=primary_source, sync_mode="delta", idx="pref-feed")
    feed_non = FeedFactory(source=non_pref_source, sync_mode="delta", idx="non-feed")

    sp_pref, rp_pref = _pushed_sp_with_rp(
        source=primary_source,
        feed=feed_pref,
        external_id="DELTA-PREF",
        sku="delta-pref-sku",
        initial_weight=Decimal("0.15"),
    )
    sp_non, rp_non = _pushed_sp_with_rp(
        source=non_pref_source,
        feed=feed_non,
        external_id="DELTA-NON",
        sku="delta-non-sku",
        initial_weight=Decimal("0.15"),
    )
    # Primary link for sp_pref's RP.
    SourceProductLink.objects.create(
        real_product_sku=rp_pref.sku, source=primary_source, is_primary=True, is_active=True
    )
    # For sp_non: a primary link belongs to a third source, sp_non itself is non-primary.
    other_pref = SourceFactory(idx="delta-race-other-pref")
    SourceProductLink.objects.create(real_product_sku=rp_non.sku, source=other_pref, is_primary=True, is_active=True)
    SourceProductLink.objects.create(
        real_product_sku=rp_non.sku, source=non_pref_source, is_primary=False, is_active=True
    )

    # Primary batch: cost change (1) + physical weight change (1) → updated_count=1, physical_updated_count=1.
    counts_pref = _run_delta(
        feed_pref, [PriceStockUpdate(external_id="DELTA-PREF", cost=Decimal("12.00"), physical={"weight": "0.25"})]
    )
    assert counts_pref["updated_count"] == 1, "cost change increments updated_count"
    assert counts_pref["physical_updated_count"] == 1
    assert counts_pref["physical_skipped_non_primary_count"] == 0
    assert counts_pref["physical_overwrite_count"] == 0
    rp_pref.refresh_from_db()
    assert rp_pref.weight == Decimal("0.25")

    # Non-primary batch: only physical change → no updated_count change, skip increment.
    counts_non = _run_delta(feed_non, [PriceStockUpdate(external_id="DELTA-NON", physical={"weight": "2.50"})])
    assert counts_non["updated_count"] == 0, "physical-only delta MUST NOT bump updated_count"
    assert counts_non["physical_updated_count"] == 0
    assert counts_non["physical_skipped_non_primary_count"] == 1
    assert counts_non["physical_overwrite_count"] == 0
    rp_non.refresh_from_db()
    assert rp_non.weight == Decimal("0.15"), "race-skipped RealProduct.weight unchanged"
    # Audit + event trail on the skip path.
    assert SourceProductChangeLog.objects.filter(
        source=ChangeLogSource.PHYSICAL_SKIPPED.value, source_product=sp_non
    ).exists()
    assert IntegrationEvent.objects.filter(
        event_type=EventType.PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY.value, source_product=sp_non
    ).exists()


def test_delta_sync_overwrite_path_counts_separately():
    """allow_physical_writes_from_non_primary=True on the non-primary source — write lands as overwrite."""
    primary_source = SourceFactory(idx="delta-overwrite-pref")
    opt_in_source = SourceFactory(idx="delta-overwrite-optin", allow_physical_writes_from_non_primary=True)
    feed = FeedFactory(source=opt_in_source, sync_mode="delta", idx="optin-feed")
    sp, rp = _pushed_sp_with_rp(
        source=opt_in_source,
        feed=feed,
        external_id="DELTA-OPTIN",
        sku="delta-overwrite-sku",
        initial_weight=Decimal("0.15"),
    )
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=primary_source, is_primary=True, is_active=True)
    SourceProductLink.objects.create(real_product_sku=rp.sku, source=opt_in_source, is_primary=False, is_active=True)

    counts = _run_delta(feed, [PriceStockUpdate(external_id="DELTA-OPTIN", physical={"weight": "3.00"})])

    assert counts["physical_overwrite_count"] == 1
    assert counts["physical_updated_count"] == 0
    assert counts["physical_skipped_non_primary_count"] == 0
    rp.refresh_from_db()
    assert rp.weight == Decimal("3.00")
    assert SourceProductChangeLog.objects.filter(
        source=ChangeLogSource.PHYSICAL_OVERWRITE.value, source_product=sp
    ).exists()
    assert IntegrationEvent.objects.filter(
        event_type=EventType.PHYSICAL_UPDATE_OVERWRITE.value, source_product=sp
    ).exists()
