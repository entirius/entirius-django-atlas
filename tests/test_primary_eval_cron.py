# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the auto-primary Celery task + management command.

`evaluate_all` is the synchronous core that both `evaluate_primary_sources_task`
(Celery beat target) and `python manage.py evaluate_primary_sources` invoke.
Idempotency is exercised explicitly: two consecutive runs should not cause a flap.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from django_atlas.enums import EvalFrequency
from django_atlas.models import SourceProductLink
from django_atlas.tasks.primary_strategy import evaluate_all, evaluate_primary_sources_task
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


def _create_pim_real_product(sku):
    from django_pim.models.real_product import RealProduct

    return RealProduct.objects.create(sku=sku)


def _link(sku, source, external_id, *, cost, stock, is_primary=False, preferred_changed_at=None):
    SourceProductFactory(source=source, external_id=external_id, cost=Decimal(str(cost)), stock=stock)
    return SourceProductLink.objects.create(
        real_product_sku=sku,
        source=source,
        external_id=external_id,
        is_primary=is_primary,
        preferred_changed_at=preferred_changed_at,
    )


def test_evaluate_all_switches_multi_source_rp():
    _create_pim_real_product("cron-rp-1")
    ft = SourceFactory(idx="ft-cron-1", primary_switch_cooldown_hours=0)  # no cooldown
    kh = SourceFactory(idx="kh-cron-1", primary_switch_cooldown_hours=0)
    _link(
        "cron-rp-1",
        ft,
        "f1",
        cost="0.14",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=48),
    )
    _link("cron-rp-1", kh, "k1", cost="0.10", stock=100)
    summary = evaluate_all()
    assert summary["evaluated"] >= 1
    assert summary["switched"] >= 1
    kh_link = SourceProductLink.objects.get(real_product_sku="cron-rp-1", source=kh)
    assert kh_link.is_primary is True


def test_evaluate_all_skips_single_source_rp():
    _create_pim_real_product("cron-rp-single")
    ft = SourceFactory(idx="ft-cron-single")
    _link("cron-rp-single", ft, "f1", cost="0.14", stock=100, is_primary=True)
    summary = evaluate_all()
    # Single-source RPs don't show up in the iteration set at all.
    assert summary["evaluated"] == 0


def test_evaluate_all_is_idempotent():
    """Run twice — the second pass must register zero new switches."""
    _create_pim_real_product("cron-rp-idem")
    ft = SourceFactory(idx="ft-cron-idem", primary_switch_cooldown_hours=0)
    kh = SourceFactory(idx="kh-cron-idem", primary_switch_cooldown_hours=0)
    _link(
        "cron-rp-idem",
        ft,
        "f1",
        cost="0.14",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=48),
    )
    _link("cron-rp-idem", kh, "k1", cost="0.10", stock=100)
    first = evaluate_all()
    second = evaluate_all()
    assert first["switched"] >= 1
    assert second["switched"] == 0


def test_evaluate_all_skips_manual_override_sku():
    _create_pim_real_product("cron-rp-manual")
    ft = SourceFactory(idx="ft-cron-manual")
    kh = SourceFactory(idx="kh-cron-manual")
    ft_link = _link("cron-rp-manual", ft, "f1", cost="0.14", stock=100, is_primary=True)
    ft_link.manual_override = True
    ft_link.save(update_fields=["manual_override"])
    _link("cron-rp-manual", kh, "k1", cost="0.10", stock=100)
    summary = evaluate_all()
    assert summary["skipped_manual_override"] >= 1
    ft_link.refresh_from_db()
    assert ft_link.is_primary is True  # unchanged


def test_evaluate_all_excludes_manual_frequency_sources():
    """A source with eval_frequency=manual opts the SKU out of cron."""
    _create_pim_real_product("cron-rp-freq")
    ft = SourceFactory(idx="ft-cron-freq", eval_frequency=EvalFrequency.MANUAL.value, primary_switch_cooldown_hours=0)
    kh = SourceFactory(idx="kh-cron-freq", eval_frequency=EvalFrequency.MANUAL.value, primary_switch_cooldown_hours=0)
    _link(
        "cron-rp-freq",
        ft,
        "f1",
        cost="0.14",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=48),
    )
    _link("cron-rp-freq", kh, "k1", cost="0.10", stock=100)
    summary = evaluate_all()
    # Both sources are manual-only → the SKU is excluded from the iteration set.
    assert summary["evaluated"] == 0


def test_celery_task_invokes_evaluate_all():
    """Smoke test the Celery task entry — calls evaluate_all and returns the summary."""
    _create_pim_real_product("cron-rp-celery")
    ft = SourceFactory(idx="ft-cron-celery", primary_switch_cooldown_hours=0)
    kh = SourceFactory(idx="kh-cron-celery", primary_switch_cooldown_hours=0)
    _link(
        "cron-rp-celery",
        ft,
        "f1",
        cost="0.14",
        stock=100,
        is_primary=True,
        preferred_changed_at=timezone.now() - timedelta(hours=48),
    )
    _link("cron-rp-celery", kh, "k1", cost="0.10", stock=100)
    result = evaluate_primary_sources_task()
    assert "evaluated" in result and "switched" in result


def test_management_command_prints_summary():
    _create_pim_real_product("cron-rp-cmd")
    ft = SourceFactory(idx="ft-cron-cmd")
    _link("cron-rp-cmd", ft, "f1", cost="0.14", stock=100, is_primary=True)
    out = StringIO()
    call_command("evaluate_primary_sources", stdout=out)
    output = out.getvalue()
    assert "Auto-primary evaluation summary" in output
    assert "evaluated" in output


def test_management_command_with_sku_argument():
    _create_pim_real_product("cron-rp-cmd-sku")
    ft = SourceFactory(idx="ft-cron-cmd-sku")
    _link("cron-rp-cmd-sku", ft, "f1", cost="0.14", stock=100, is_primary=True)
    out = StringIO()
    call_command("evaluate_primary_sources", "--sku", "cron-rp-cmd-sku", stdout=out)
    output = out.getvalue()
    assert "cron-rp-cmd-sku" in output
