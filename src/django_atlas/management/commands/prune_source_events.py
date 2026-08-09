# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Prune IntegrationEvent rows older than SourceSettings.integration_event_retention_days."""

from django.core.management.base import BaseCommand, CommandError

from django_atlas.models import SourceSettings
from django_atlas.services import event_service


class Command(BaseCommand):
    help = "Delete non-critical IntegrationEvent rows older than the retention window."

    def handle(self, *args, **options) -> None:
        settings = SourceSettings.load()
        days = settings.integration_event_retention_days
        try:
            deleted = event_service.prune_older_than(days)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Deleted {deleted} events older than {days} days.")
