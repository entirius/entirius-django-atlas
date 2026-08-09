# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retention coverage for prune_source_change_logs_task + management command."""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from django_atlas.enums import ChangeLogSource
from django_atlas.models import SourceProductChangeLog, SourceSettings
from django_atlas.services import audit_service
from django_atlas.tasks.audit_retention import prune_source_change_logs_task
from tests.factories import SourceProductFactory

pytestmark = pytest.mark.django_db


def _make_old_entry(sp, days_old: int) -> SourceProductChangeLog:
    entry = audit_service.log_change(
        source_product=sp, source=ChangeLogSource.FULL_SYNC.value, field_path="cost", before=1, after=2
    )
    SourceProductChangeLog.objects.filter(pk=entry.pk).update(created_at=timezone.now() - timedelta(days=days_old))
    return entry


def test_prune_task_uses_settings_retention_window():
    sp = SourceProductFactory()
    settings = SourceSettings.load()
    settings.change_log_retention_days = 30
    settings.save()
    _make_old_entry(sp, days_old=45)  # to delete
    _make_old_entry(sp, days_old=10)  # to keep
    result = prune_source_change_logs_task()
    assert result == {"deleted": 1, "retention_days": 30}
    assert SourceProductChangeLog.objects.count() == 1


def test_prune_command_with_days_override_zero_deletes_everything():
    sp = SourceProductFactory()
    audit_service.log_change(
        source_product=sp, source=ChangeLogSource.DELTA_SYNC.value, field_path="stock", before=1, after=2
    )
    call_command("prune_source_change_logs", "--days", "0")
    assert SourceProductChangeLog.objects.count() == 0
