# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for primary_strategy_service."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType, PrimarySkipReason
from django_atlas.models import IntegrationEvent, SourceProductChangeLog, SourceProductLink
from django_atlas.services import primary_strategy_service
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _create_pim_real_product(sku="rp-1"):
    from django_pim.models.real_product import RealProduct

    return RealProduct.objects.create(sku=sku)


def _link_source(sku, source, external_id, *, cost, stock, is_primary=False, preferred_changed_at=None):
    """Helper: create SP + SourceProductLink with matching external_id."""
    sp = SourceProductFactory(source=source, external_id=external_id, cost=Decimal(str(cost)), stock=stock)
    link = SourceProductLink.objects.create(
        real_product_sku=sku,
        source=source,
        external_id=external_id,
        is_primary=is_primary,
        preferred_changed_at=preferred_changed_at,
    )
    return sp, link


def test_evaluate_single_link_returns_no_candidates_skip():
    _create_pim_real_product("rp-single")
    sup = SourceFactory(idx="ft")
    _link_source("rp-single", sup, "ft-1", cost="0.14", stock=100, is_primary=True)
    result = primary_strategy_service.evaluate_primary_source("rp-single")
    # Only one link → second source needed for a real eval; here winner == current
    # so the result is NO_CHANGE (the link IS the candidate).
    assert result.should_switch is False
    assert result.skip_reason == PrimarySkipReason.NO_CHANGE
    assert result.new_primary.source.idx == "ft"


def test_evaluate_two_links_no_current_primary_selects_lowest_cost():
    _create_pim_real_product("rp-2a")
    ft = SourceFactory(idx="ft-2a")
    kh = SourceFactory(idx="kh-2a")
    _link_source("rp-2a", ft, "ft-1", cost="0.14", stock=100)
    _link_source("rp-2a", kh, "kh-1", cost="0.13", stock=100)
    result = primary_strategy_service.evaluate_primary_source("rp-2a")
    assert result.should_switch is True
    assert result.new_primary.source.idx == "kh-2a"
    assert result.current_primary is None


def test_evaluate_hysteresis_blocks_small_improvement():
    _create_pim_real_product("rp-hyst")
    ft = SourceFactory(idx="ft-hyst", primary_switch_hysteresis_pct=2)
    kh = SourceFactory(idx="kh-hyst", primary_switch_hysteresis_pct=2)
    _link_source("rp-hyst", ft, "ft-1", cost="0.140", stock=100, is_primary=True)
    _link_source("rp-hyst", kh, "kh-1", cost="0.139", stock=100)  # 0.71% diff
    event_sink: list[dict] = []
    result = primary_strategy_service.evaluate_primary_source("rp-hyst", event_sink=event_sink)
    assert result.should_switch is False
    assert result.skip_reason == PrimarySkipReason.HYSTERESIS
    assert any(e["event_type"] == EventType.PRIMARY_SWITCH_SKIPPED_HYSTERESIS.value for e in event_sink)


def test_evaluate_cost_improvement_above_hysteresis_returns_should_switch():
    _create_pim_real_product("rp-big")
    ft = SourceFactory(idx="ft-big", primary_switch_hysteresis_pct=2)
    kh = SourceFactory(idx="kh-big", primary_switch_hysteresis_pct=2)
    _link_source(
        "rp-big",
        ft,
        "ft-1",
        cost="0.14",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=48),
    )
    _link_source("rp-big", kh, "kh-1", cost="0.13", stock=100)
    result = primary_strategy_service.evaluate_primary_source("rp-big")
    assert result.should_switch is True
    assert result.new_primary.source.idx == "kh-big"


def test_evaluate_cooldown_blocks_fresh_switch():
    _create_pim_real_product("rp-cool")
    ft = SourceFactory(idx="ft-cool", primary_switch_cooldown_hours=24)
    kh = SourceFactory(idx="kh-cool", primary_switch_cooldown_hours=24)
    _link_source(
        "rp-cool",
        ft,
        "ft-1",
        cost="0.20",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=2),
    )
    _link_source("rp-cool", kh, "kh-1", cost="0.10", stock=100)  # 50% cheaper
    event_sink: list[dict] = []
    result = primary_strategy_service.evaluate_primary_source("rp-cool", event_sink=event_sink)
    assert result.should_switch is False
    assert result.skip_reason == PrimarySkipReason.COOLDOWN
    assert any(e["event_type"] == EventType.PRIMARY_SWITCH_SKIPPED_COOLDOWN.value for e in event_sink)


def test_evaluate_cooldown_bypassed_in_emergency():
    _create_pim_real_product("rp-emerg")
    ft = SourceFactory(idx="ft-emerg", primary_switch_cooldown_hours=24)
    kh = SourceFactory(idx="kh-emerg", primary_switch_cooldown_hours=24)
    _link_source(
        "rp-emerg",
        ft,
        "ft-1",
        cost="0.20",
        stock=0,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=2),
    )
    _link_source("rp-emerg", kh, "kh-1", cost="0.10", stock=100)
    result = primary_strategy_service.evaluate_primary_source("rp-emerg", bypass_safety=True)
    assert result.should_switch is True
    assert result.new_primary.source.idx == "kh-emerg"


def test_evaluate_manual_override_sticky_blocks_eval():
    _create_pim_real_product("rp-manual")
    ft = SourceFactory(idx="ft-manual")
    kh = SourceFactory(idx="kh-manual")
    _, ft_link = _link_source("rp-manual", ft, "ft-1", cost="0.20", stock=100, is_primary=True)
    ft_link.manual_override = True
    ft_link.save(update_fields=["manual_override"])
    _link_source("rp-manual", kh, "kh-1", cost="0.10", stock=100)
    event_sink: list[dict] = []
    result = primary_strategy_service.evaluate_primary_source("rp-manual", event_sink=event_sink)
    assert result.should_switch is False
    assert result.skip_reason == PrimarySkipReason.MANUAL_OVERRIDE
    assert any(e["event_type"] == EventType.PRIMARY_SWITCH_SKIPPED_MANUAL_OVERRIDE.value for e in event_sink)


def test_evaluate_no_candidates_when_all_out_of_stock():
    _create_pim_real_product("rp-empty")
    ft = SourceFactory(idx="ft-empty")
    kh = SourceFactory(idx="kh-empty")
    _link_source("rp-empty", ft, "ft-1", cost="0.14", stock=0, is_primary=True)
    _link_source("rp-empty", kh, "kh-1", cost="0.13", stock=0)
    result = primary_strategy_service.evaluate_primary_source("rp-empty")
    assert result.should_switch is False
    assert result.skip_reason == PrimarySkipReason.NO_CANDIDATES


def test_apply_primary_switch_flips_atomically_and_audits():
    _create_pim_real_product("rp-apply")
    ft = SourceFactory(idx="ft-apply")
    kh = SourceFactory(idx="kh-apply")
    _, ft_link = _link_source("rp-apply", ft, "ft-1", cost="0.14", stock=100, is_primary=True)
    _, kh_link = _link_source("rp-apply", kh, "kh-1", cost="0.13", stock=100)
    primary_strategy_service.apply_primary_switch(
        new_link=kh_link,
        previous_link=ft_link,
        reason_source=ChangeLogSource.AUTO_PRIMARY_SWITCH,
        event_type=EventType.PRIMARY_SOURCE_SWITCHED,
        severity=EventSeverity.INFO,
        decision_audit={"strategy": "lowest_cost_with_stock"},
    )
    ft_link.refresh_from_db()
    kh_link.refresh_from_db()
    assert ft_link.is_primary is False
    assert kh_link.is_primary is True
    assert kh_link.preferred_changed_at is not None
    assert (
        SourceProductChangeLog.objects.filter(
            real_product_sku="rp-apply", source=ChangeLogSource.AUTO_PRIMARY_SWITCH.value
        ).count()
        == 1
    )
    assert IntegrationEvent.objects.filter(event_type=EventType.PRIMARY_SOURCE_SWITCHED.value).count() == 1


def test_force_set_primary_sets_manual_override_and_emits_warning_event():
    _create_pim_real_product("rp-force")
    ft = SourceFactory(idx="ft-force")
    kh = SourceFactory(idx="kh-force")
    _, ft_link = _link_source("rp-force", ft, "ft-1", cost="0.14", stock=100)
    _, kh_link = _link_source("rp-force", kh, "kh-1", cost="0.13", stock=100, is_primary=True)
    result = primary_strategy_service.force_set_primary(
        real_product_sku="rp-force", source_idx="ft-force", reason="strategic partner"
    )
    ft_link.refresh_from_db()
    kh_link.refresh_from_db()
    assert result.pk == ft_link.pk
    assert ft_link.is_primary is True
    assert ft_link.manual_override is True
    assert kh_link.is_primary is False
    assert kh_link.manual_override is False
    assert IntegrationEvent.objects.filter(event_type=EventType.PRIMARY_SOURCE_FORCED.value).count() == 1


def test_force_set_primary_rejects_short_reason():
    _create_pim_real_product("rp-force-bad")
    ft = SourceFactory(idx="ft-force-bad")
    _link_source("rp-force-bad", ft, "ft-1", cost="0.14", stock=100)
    with pytest.raises(ValueError, match="reason"):
        primary_strategy_service.force_set_primary(
            real_product_sku="rp-force-bad", source_idx="ft-force-bad", reason="ok"
        )


def test_reset_to_auto_clears_manual_override_and_re_evaluates():
    _create_pim_real_product("rp-reset")
    ft = SourceFactory(idx="ft-reset")
    kh = SourceFactory(idx="kh-reset")
    _, ft_link = _link_source("rp-reset", ft, "ft-1", cost="0.20", stock=100, is_primary=True)
    ft_link.manual_override = True
    ft_link.save(update_fields=["manual_override"])
    _, kh_link = _link_source("rp-reset", kh, "kh-1", cost="0.10", stock=100)
    result = primary_strategy_service.reset_to_auto(real_product_sku="rp-reset")
    ft_link.refresh_from_db()
    kh_link.refresh_from_db()
    assert ft_link.manual_override is False
    assert ft_link.is_primary is False
    assert kh_link.is_primary is True
    assert result.should_switch is True
    assert result.new_primary.source.idx == "kh-reset"


def test_iter_multi_source_skus_returns_only_multi_link():
    _create_pim_real_product("rp-multi")
    _create_pim_real_product("rp-single")
    sup_a = SourceFactory(idx="sup-a")
    sup_b = SourceFactory(idx="sup-b")
    _link_source("rp-multi", sup_a, "a-1", cost="0.1", stock=10)
    _link_source("rp-multi", sup_b, "b-1", cost="0.2", stock=20)
    _link_source("rp-single", sup_a, "a-2", cost="0.3", stock=30)
    skus = primary_strategy_service.iter_multi_source_real_product_skus()
    assert "rp-multi" in skus
    assert "rp-single" not in skus
