# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for audit_service: log_change / log_changes_bulk / prune_older_than."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from django_atlas.enums import ChangeLogSource
from django_atlas.models import SourceProductChangeLog
from django_atlas.services import audit_service
from tests.factories import SourceProductFactory

pytestmark = pytest.mark.django_db


def test_log_change_creates_row_with_user_and_decimal_after():
    sp = SourceProductFactory()
    user = User.objects.create_user(username="u", password="p")
    entry = audit_service.log_change(
        source_product=sp,
        source=ChangeLogSource.OPERATOR_SP_EDIT.value,
        field_path="cost",
        before=Decimal("10.00"),
        after=Decimal("12.50"),
        triggered_by=user,
    )
    assert entry.pk is not None
    assert entry.source == "operator_sp_edit"
    assert entry.field_path == "cost"
    assert entry.triggered_by == user
    assert entry.applied_to_pim is False


def test_log_change_invalid_source_raises():
    sp = SourceProductFactory()
    with pytest.raises(ValueError, match="not in whitelist"):
        audit_service.log_change(source_product=sp, source="bogus_source", field_path="cost", before=None, after=None)


def test_log_change_empty_field_path_raises():
    sp = SourceProductFactory()
    with pytest.raises(ValueError, match="field_path is required"):
        audit_service.log_change(
            source_product=sp, source=ChangeLogSource.FULL_SYNC.value, field_path="", before=1, after=2
        )


def test_log_change_applied_to_pim_stamps_timestamp():
    sp = SourceProductFactory()
    entry = audit_service.log_change(
        source_product=sp, source=ChangeLogSource.FORCE_REPUSH.value, field_path="attribute.color", applied_to_pim=True
    )
    assert entry.applied_to_pim_at is not None


def test_log_changes_bulk_single_query(django_assert_num_queries):
    sp = SourceProductFactory()
    drafts = [
        {
            "source_product": sp,
            "source": ChangeLogSource.FULL_SYNC.value,
            "field_path": f"data.field_{i}",
            "before": i,
            "after": i + 1,
        }
        for i in range(5)
    ]
    with django_assert_num_queries(1):
        count = audit_service.log_changes_bulk(drafts)
    assert count == 5
    assert SourceProductChangeLog.objects.count() == 5


def test_log_changes_bulk_invalid_draft_raises_with_index():
    sp = SourceProductFactory()
    drafts = [
        {"source_product": sp, "source": ChangeLogSource.FULL_SYNC.value, "field_path": "cost"},
        {"source_product": sp, "source": "bogus", "field_path": "stock"},
    ]
    with pytest.raises(ValueError, match=r"drafts\[1\]"):
        audit_service.log_changes_bulk(drafts)
    assert SourceProductChangeLog.objects.count() == 0


def test_prune_older_than_deletes_old_keeps_recent():
    sp = SourceProductFactory()
    old = audit_service.log_change(
        source_product=sp, source=ChangeLogSource.FULL_SYNC.value, field_path="cost", before=1, after=2
    )
    recent = audit_service.log_change(
        source_product=sp, source=ChangeLogSource.FULL_SYNC.value, field_path="stock", before=5, after=6
    )
    SourceProductChangeLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=120))
    deleted = audit_service.prune_older_than(90)
    assert deleted == 1
    assert SourceProductChangeLog.objects.filter(pk=recent.pk).exists()


def test_prune_older_than_negative_raises():
    with pytest.raises(ValueError, match="days must be >= 0"):
        audit_service.prune_older_than(-1)


@pytest.mark.parametrize(
    "source",
    [
        ChangeLogSource.COST_SIGNAL_RECEIVED.value,
        ChangeLogSource.COST_IGNORED_NON_PRIMARY.value,
        ChangeLogSource.COST_IGNORED_NO_LINK.value,
        ChangeLogSource.COST_SKIPPED_ADMIN_OVERRIDE.value,
        ChangeLogSource.COST_SKIPPED_RESOLUTION_FAILED.value,
    ],
)
def test_log_change_accepts_etap11_cost_sources(source):
    """pricemanager subscriber writes audit rows with these source codes."""
    sp = SourceProductFactory()
    entry = audit_service.log_change(
        source_product=sp, source=source, field_path="current_price.net_value[channel=x]", before=None, after="0.13"
    )
    assert entry.source == source
