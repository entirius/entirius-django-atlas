# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from decimal import Decimal

import pytest

from django_atlas.enums import ProductStatus
from django_atlas.services import review_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Valid transitions (1-10)
# ---------------------------------------------------------------------------


def test_new_to_queued(admin_user):
    sp = SourceProductFactory(status=ProductStatus.NEW.value)

    review_service.queue(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.QUEUED.value
    assert sp.reviewed_by_id == admin_user.pk


def test_new_to_approved(admin_user):
    sp = SourceProductFactory(status=ProductStatus.NEW.value)

    review_service.approve(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.APPROVED.value
    assert sp.reviewed_by_id == admin_user.pk


def test_new_to_rejected(admin_user):
    sp = SourceProductFactory(status=ProductStatus.NEW.value)

    review_service.reject(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.REJECTED.value


def test_queued_to_approved_sets_audit(admin_user):
    sp = SourceProductFactory(status=ProductStatus.QUEUED.value)

    review_service.approve(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.APPROVED.value
    assert sp.reviewed_by_id == admin_user.pk
    assert sp.reviewed_at is not None


def test_queued_to_rejected_sets_audit(admin_user):
    sp = SourceProductFactory(status=ProductStatus.QUEUED.value)

    review_service.reject(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.REJECTED.value
    assert sp.reviewed_by_id == admin_user.pk


def test_queued_to_new_via_skip_keeps_reviewed_by(admin_user, regular_user):
    sp = SourceProductFactory(status=ProductStatus.QUEUED.value, reviewed_by=regular_user)

    review_service.skip(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.NEW.value
    assert sp.reviewed_by_id == regular_user.pk


def test_rejected_to_queued_re_review(admin_user):
    sp = SourceProductFactory(status=ProductStatus.REJECTED.value)

    review_service.queue(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.QUEUED.value


def test_approved_to_rejected(admin_user):
    sp = SourceProductFactory(status=ProductStatus.APPROVED.value)

    review_service.reject(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.REJECTED.value


def test_pushed_pending_images_to_pushed():
    sp = SourceProductFactory(status=ProductStatus.PUSHED_PENDING_IMAGES.value)

    review_service.transition_status(sp, ProductStatus.PUSHED.value)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.PUSHED.value


def test_pushed_to_approved_force_repush_keeps_reviewed_by(admin_user, regular_user):
    sp = SourceProductFactory(status=ProductStatus.PUSHED.value, reviewed_by=regular_user)
    original_reviewed_at = sp.reviewed_at

    review_service.transition_status(sp, ProductStatus.APPROVED.value, user=admin_user)

    sp.refresh_from_db()
    assert sp.status == ProductStatus.APPROVED.value
    assert sp.reviewed_by_id == regular_user.pk
    assert sp.reviewed_at is not None
    assert sp.reviewed_at != original_reviewed_at


# ---------------------------------------------------------------------------
# Invalid transitions (11-15)
# ---------------------------------------------------------------------------


def test_new_to_pushed_raises():
    sp = SourceProductFactory(status=ProductStatus.NEW.value)
    with pytest.raises(ValueError, match="invalid transition"):
        review_service.transition_status(sp, ProductStatus.PUSHED.value)


def test_new_to_pushed_pending_images_raises():
    sp = SourceProductFactory(status=ProductStatus.NEW.value)
    with pytest.raises(ValueError, match="invalid transition"):
        review_service.transition_status(sp, ProductStatus.PUSHED_PENDING_IMAGES.value)


def test_rejected_to_pushed_raises():
    sp = SourceProductFactory(status=ProductStatus.REJECTED.value)
    with pytest.raises(ValueError, match="invalid transition"):
        review_service.transition_status(sp, ProductStatus.PUSHED.value)


def test_rejected_to_approved_raises():
    sp = SourceProductFactory(status=ProductStatus.REJECTED.value)
    with pytest.raises(ValueError, match="invalid transition"):
        review_service.transition_status(sp, ProductStatus.APPROVED.value)


def test_pushed_to_rejected_raises():
    sp = SourceProductFactory(status=ProductStatus.PUSHED.value)
    with pytest.raises(ValueError, match="invalid transition"):
        review_service.transition_status(sp, ProductStatus.REJECTED.value)


# ---------------------------------------------------------------------------
# Audit fields (16)
# ---------------------------------------------------------------------------


def test_approve_sets_reviewed_by_and_at(admin_user):
    sp = SourceProductFactory(status=ProductStatus.QUEUED.value)

    review_service.approve(sp.pk, admin_user)

    sp.refresh_from_db()
    assert sp.reviewed_by_id == admin_user.pk
    assert sp.reviewed_at is not None


# ---------------------------------------------------------------------------
# Bulk approve / reject (17-18)
# ---------------------------------------------------------------------------


def test_bulk_approve_mixed_statuses(admin_user):
    source = SourceFactory()
    queued_a = SourceProductFactory(source=source, status=ProductStatus.QUEUED.value)
    queued_b = SourceProductFactory(source=source, status=ProductStatus.QUEUED.value)
    new_a = SourceProductFactory(source=source, status=ProductStatus.NEW.value)
    new_b = SourceProductFactory(source=source, status=ProductStatus.NEW.value)
    rejected = SourceProductFactory(source=source, status=ProductStatus.REJECTED.value)

    result = review_service.bulk_approve([queued_a.pk, queued_b.pk, new_a.pk, new_b.pk, rejected.pk], admin_user)

    assert result["success"] == 4
    assert result["invalid_transition"] == 1
    assert result["ids_failed"] == [rejected.pk]


def test_bulk_reject_sets_status_and_audit(admin_user):
    sps = [SourceProductFactory(status=ProductStatus.QUEUED.value) for _ in range(3)]

    result = review_service.bulk_reject([sp.pk for sp in sps], admin_user)

    assert result["success"] == 3
    for sp in sps:
        sp.refresh_from_db()
        assert sp.status == ProductStatus.REJECTED.value
        assert sp.reviewed_at is not None


# ---------------------------------------------------------------------------
# List filtering (19-20)
# ---------------------------------------------------------------------------


def test_list_for_review_filters_by_status_and_source():
    s1 = SourceFactory()
    s2 = SourceFactory()
    SourceProductFactory(source=s1, status=ProductStatus.QUEUED.value)
    SourceProductFactory(source=s1, status=ProductStatus.NEW.value)
    SourceProductFactory(source=s2, status=ProductStatus.QUEUED.value)

    qs = review_service.list_for_review(source_id=s1.pk, status=ProductStatus.QUEUED.value)

    assert qs.count() == 1


def test_list_for_review_filters_by_cost_range():
    SourceProductFactory(status=ProductStatus.QUEUED.value, cost=Decimal("5.00"))
    SourceProductFactory(status=ProductStatus.QUEUED.value, cost=Decimal("50.00"))
    SourceProductFactory(status=ProductStatus.QUEUED.value, cost=Decimal("500.00"))

    qs = review_service.list_for_review(cost_min=Decimal("10"), cost_max=Decimal("100"))

    assert qs.count() == 1
    assert qs.first().cost == Decimal("50.00")


def test_list_for_review_ordering_accepts_allowlisted_field():
    SourceProductFactory(status=ProductStatus.QUEUED.value, cost=Decimal("5.00"))
    SourceProductFactory(status=ProductStatus.QUEUED.value, cost=Decimal("50.00"))

    qs = review_service.list_for_review(ordering="-cost")

    assert [sp.cost for sp in qs] == [Decimal("50.00"), Decimal("5.00")]


def test_list_for_review_ordering_rejects_unknown_field():
    with pytest.raises(ValueError, match="Invalid ordering field"):
        review_service.list_for_review(ordering="source__credentials")


# ---------------------------------------------------------------------------
# Decision #29 — bulk_requeue preserves reviewed_by (21-22)
# ---------------------------------------------------------------------------


def test_bulk_requeue_preserves_reviewed_by(admin_user, regular_user):
    sps = [SourceProductFactory(status=ProductStatus.REJECTED.value, reviewed_by=regular_user) for _ in range(4)]

    result = review_service.bulk_requeue([sp.pk for sp in sps], admin_user)

    assert result["success"] == 4
    for sp in sps:
        sp.refresh_from_db()
        assert sp.status == ProductStatus.QUEUED.value
        assert sp.reviewed_by_id == regular_user.pk


def test_bulk_requeue_skips_non_rejected(admin_user):
    rejected = [SourceProductFactory(status=ProductStatus.REJECTED.value) for _ in range(3)]
    queued = SourceProductFactory(status=ProductStatus.QUEUED.value)
    approved = SourceProductFactory(status=ProductStatus.APPROVED.value)

    result = review_service.bulk_requeue([sp.pk for sp in rejected] + [queued.pk, approved.pk], admin_user)

    assert result["success"] == 3
    assert result["invalid_transition"] == 2
    assert set(result["ids_failed"]) == {queued.pk, approved.pk}
    queued.refresh_from_db()
    approved.refresh_from_db()
    assert queued.status == ProductStatus.QUEUED.value
    assert approved.status == ProductStatus.APPROVED.value
