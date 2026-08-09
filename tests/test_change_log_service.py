# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for change_log_service."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from django_atlas.enums import ChangeLogSource
from django_atlas.models import SourceProductChangeLog, SourceProductLink
from django_atlas.services import audit_service, change_log_service
from tests.factories import SourceProductFactory

pytestmark = pytest.mark.django_db


def _make_log(sp, *, sku, source=ChangeLogSource.DELTA_SYNC, field_path="cost", applied=False, days_ago=0):
    entry = audit_service.log_change(
        source_product=sp,
        source=source.value,
        field_path=field_path,
        before=1,
        after=2,
        applied_to_pim=applied,
        real_product_sku=sku,
    )
    if days_ago:
        SourceProductChangeLog.objects.filter(pk=entry.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
    return entry


def _link_sku(source, sku, *, is_primary=False):
    return SourceProductLink.objects.create(real_product_sku=sku, source=source, is_primary=is_primary, is_active=True)


# ---------------------------------------------------------------------------
# list_for_sku
# ---------------------------------------------------------------------------


def test_list_for_sku_not_found_when_no_link_and_no_logs():
    with pytest.raises(ValueError, match="not found"):
        change_log_service.list_for_sku("UNKNOWN-SKU")


def test_list_for_sku_returns_has_source_with_source_payload():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-1", is_primary=True)
    _make_log(sp, sku="AC-1")
    payload = change_log_service.list_for_sku("AC-1")
    assert payload["has_source"] is True
    assert payload["source"] == {"idx": sp.source.idx, "name": sp.source.name}
    assert payload["unseen_count"] == 1
    assert len(payload["changes"]) == 1


def test_list_for_sku_filters_unseen_only():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-2")
    _make_log(sp, sku="AC-2", applied=False)
    _make_log(sp, sku="AC-2", applied=True, field_path="stock")
    payload = change_log_service.list_for_sku("AC-2", unseen_only=True)
    assert payload["unseen_count"] == 1
    assert len(payload["changes"]) == 1
    assert payload["changes"][0]["applied_to_pim"] is False


def test_list_for_sku_filters_by_source():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-3")
    _make_log(sp, sku="AC-3", source=ChangeLogSource.FULL_SYNC, field_path="name")
    _make_log(sp, sku="AC-3", source=ChangeLogSource.DELTA_SYNC, field_path="cost")
    payload = change_log_service.list_for_sku("AC-3", sources=["delta_sync"])
    assert len(payload["changes"]) == 1
    assert payload["changes"][0]["source"] == "delta_sync"


def test_list_for_sku_rejects_unknown_source_value():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-4")
    _make_log(sp, sku="AC-4")
    with pytest.raises(ValueError, match="Unknown source"):
        change_log_service.list_for_sku("AC-4", sources=["bogus"])


def test_list_for_sku_filters_by_since():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-5")
    _make_log(sp, sku="AC-5", days_ago=10)
    _make_log(sp, sku="AC-5", field_path="stock")  # today
    since = timezone.now() - timedelta(days=1)
    payload = change_log_service.list_for_sku("AC-5", since=since)
    assert len(payload["changes"]) == 1
    assert payload["changes"][0]["field_path"] == "stock"


def test_list_for_sku_returns_200_when_link_exists_but_no_changes():
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-6")
    payload = change_log_service.list_for_sku("AC-6")
    assert payload["has_source"] is True
    assert payload["unseen_count"] == 0
    assert payload["changes"] == []
    assert payload["last_change_at"] is None


# ---------------------------------------------------------------------------
# bulk_has_changes
# ---------------------------------------------------------------------------


def test_bulk_has_changes_empty_list_returns_empty():
    assert change_log_service.bulk_has_changes([]) == {}


def test_bulk_has_changes_unknown_sku_returns_no_source_entry():
    result = change_log_service.bulk_has_changes(["UNKNOWN-A", "UNKNOWN-B"])
    assert set(result) == {"UNKNOWN-A", "UNKNOWN-B"}
    assert result["UNKNOWN-A"]["has_source"] is False
    assert result["UNKNOWN-A"]["unseen_count"] == 0


def test_bulk_has_changes_combines_link_and_audit_aggregate():
    sp1 = SourceProductFactory()
    sp2 = SourceProductFactory()
    _link_sku(sp1.source, "AC-A", is_primary=True)
    _link_sku(sp2.source, "AC-B")
    _make_log(sp1, sku="AC-A", applied=False)
    _make_log(sp1, sku="AC-A", applied=False, field_path="stock")
    _make_log(sp1, sku="AC-A", applied=True, field_path="weight")
    _make_log(sp2, sku="AC-B", applied=False)
    result = change_log_service.bulk_has_changes(["AC-A", "AC-B", "UNKNOWN"])
    assert result["AC-A"]["has_source"] is True
    assert result["AC-A"]["source_idx"] == sp1.source.idx
    assert result["AC-A"]["unseen_count"] == 2
    assert result["AC-B"]["has_source"] is True
    assert result["AC-B"]["unseen_count"] == 1
    assert result["UNKNOWN"]["has_source"] is False
    assert result["UNKNOWN"]["unseen_count"] == 0


def test_bulk_has_changes_uses_few_queries(django_assert_max_num_queries):
    sp = SourceProductFactory()
    for i in range(20):
        sku = f"AC-{i:03d}"
        _link_sku(sp.source, sku)
        _make_log(sp, sku=sku, applied=False)
    skus = [f"AC-{i:03d}" for i in range(20)]
    with django_assert_max_num_queries(3):
        result = change_log_service.bulk_has_changes(skus)
    assert len(result) == 20


# ---------------------------------------------------------------------------
# acknowledge
# ---------------------------------------------------------------------------


def test_acknowledge_rejects_both_modes_set():
    with pytest.raises(ValueError, match="not both"):
        change_log_service.acknowledge("AC-1", change_ids=[1], all_unseen=True)


def test_acknowledge_rejects_neither_mode_set():
    with pytest.raises(ValueError, match="Specify exactly one"):
        change_log_service.acknowledge("AC-1", change_ids=None, all_unseen=False)


def test_acknowledge_all_unseen_flips_only_unseen_rows():
    sp = SourceProductFactory()
    _make_log(sp, sku="AC-Z", applied=False)
    _make_log(sp, sku="AC-Z", applied=False, field_path="stock")
    _make_log(sp, sku="AC-Z", applied=True, field_path="weight")
    count = change_log_service.acknowledge("AC-Z", all_unseen=True)
    assert count == 2
    assert SourceProductChangeLog.objects.filter(real_product_sku="AC-Z", applied_to_pim=False).count() == 0
    # audit-of-audit row created
    assert SourceProductChangeLog.objects.filter(source=ChangeLogSource.OPERATOR_ACKNOWLEDGE.value).count() == 1


def test_acknowledge_change_ids_validates_membership():
    sp = SourceProductFactory()
    other_sp = SourceProductFactory()
    own = _make_log(sp, sku="AC-OWN", applied=False)
    foreign = _make_log(other_sp, sku="AC-OTHER", applied=False)
    with pytest.raises(ValueError, match="do not belong"):
        change_log_service.acknowledge("AC-OWN", change_ids=[own.pk, foreign.pk])


def test_acknowledge_zero_rows_does_not_emit_audit_entry():
    sp = SourceProductFactory()
    _make_log(sp, sku="AC-NONE", applied=True)
    count = change_log_service.acknowledge("AC-NONE", all_unseen=True)
    assert count == 0
    assert SourceProductChangeLog.objects.filter(source=ChangeLogSource.OPERATOR_ACKNOWLEDGE.value).count() == 0


def test_acknowledge_records_user_in_audit_entry():
    sp = SourceProductFactory()
    user = User.objects.create_user(username="ack-op", password="p")
    _make_log(sp, sku="AC-U", applied=False)
    change_log_service.acknowledge("AC-U", all_unseen=True, user=user)
    audit_row = SourceProductChangeLog.objects.get(source=ChangeLogSource.OPERATOR_ACKNOWLEDGE.value)
    assert audit_row.triggered_by == user
    assert audit_row.applied_to_pim is True
    assert audit_row.after["count"] == 1


# ---------------------------------------------------------------------------
# force_repush_by_sku
# ---------------------------------------------------------------------------


def test_force_repush_by_sku_no_links_raises_not_found():
    with pytest.raises(ValueError, match="not found"):
        change_log_service.force_repush_by_sku("UNKNOWN", user=None)


def test_force_repush_by_sku_delegates_per_link(monkeypatch):
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-FR")
    calls: list[int] = []

    def fake_force(sp_id: int, user):
        calls.append(sp_id)
        return ["chan1", "chan2"]

    monkeypatch.setattr("django_atlas.services.change_log_service.push_service.force_repush_source_product", fake_force)
    # Need RealProduct.sku = AC-FR linked to sp — simulate by attaching a stub
    sp.real_product = None  # ensure lookup via sp linkage by source; service falls back

    # Force the lookup to find sp_id via SourceProduct query — we monkeypatch that too
    def fake_values_list(self, *fields, **kw):  # noqa: ARG001
        from django_atlas.models import SourceProduct as SP

        return SP.objects.filter(pk=sp.pk).values_list(*fields, **kw)

    # Simpler approach: link the real_product on the SP via PIM
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="AC-FR")
    sp.real_product = rp
    sp.save(update_fields=["real_product"])

    result = change_log_service.force_repush_by_sku("AC-FR", user=None)
    assert calls == [sp.pk]
    assert result["processed_sp_ids"] == [sp.pk]
    assert result["pushed_channels_count"] == 2
    assert result["failed"] == []


def test_force_repush_by_sku_captures_failures(monkeypatch):
    sp = SourceProductFactory()
    _link_sku(sp.source, "AC-FAIL")
    from django_pim.models.real_product import RealProduct

    rp = RealProduct.objects.create(sku="AC-FAIL")
    sp.real_product = rp
    sp.save(update_fields=["real_product"])

    def fake_force(sp_id: int, user):
        raise ValueError("not in pushed status")

    monkeypatch.setattr("django_atlas.services.change_log_service.push_service.force_repush_source_product", fake_force)
    result = change_log_service.force_repush_by_sku("AC-FAIL", user=None)
    assert result["processed_sp_ids"] == []
    assert len(result["failed"]) == 1
    assert "not in pushed status" in result["failed"][0]["reason"]
