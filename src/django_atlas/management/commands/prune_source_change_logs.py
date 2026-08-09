# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Prune SourceProductChangeLog rows older than retention window.

Default uses `SourceSettings.change_log_retention_days` (90 days at install).
Override with `--days N` for one-off cleanup or testing.
"""

from django.core.management.base import BaseCommand, CommandError

from django_atlas.models import SourceSettings
from django_atlas.services import audit_service


class Command(BaseCommand):
    help = "Delete SourceProductChangeLog rows older than the retention window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days", type=int, default=None, help="Override SourceSettings.change_log_retention_days for this run."
        )

    def handle(self, *args, **options) -> None:
        days_override = options.get("days")
        if days_override is None:
            settings = SourceSettings.load()
            days = settings.change_log_retention_days
        else:
            days = days_override
        try:
            deleted = audit_service.prune_older_than(days)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Deleted {deleted} change log entries older than {days} days.")
