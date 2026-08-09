# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Print high-level operational status for the sources module."""

from django.core.management.base import BaseCommand
from django.db.models import Count

from django_atlas.enums import EventSeverity
from django_atlas.models import ImportLog, IntegrationEvent, Source, SourceFeed, SourceSettings


class Command(BaseCommand):
    help = "Show sources status: settings, counts, recent imports, unack events."

    def handle(self, *args, **options) -> None:
        settings = SourceSettings.load()
        self.stdout.write("=== Source Settings ===")
        self.stdout.write(f"  auto_push_enabled: {settings.auto_push_enabled}")
        self.stdout.write(f"  scraper_dispatch_enabled: {settings.scraper_dispatch_enabled}")
        self.stdout.write(f"  delta_sync_enabled: {settings.delta_sync_enabled}")
        self.stdout.write(f"  integration_event_retention_days: {settings.integration_event_retention_days}")

        active_sources = Source.objects.filter(is_active=True).count()
        active_feeds = SourceFeed.objects.filter(is_active=True).count()
        self.stdout.write(f"\nActive sources: {active_sources}")
        self.stdout.write(f"Active feeds: {active_feeds}")

        self.stdout.write("\n=== Recent Import Logs (top 5) ===")
        recent = ImportLog.objects.select_related("feed").order_by("-started_at")[:5]
        if not recent:
            self.stdout.write("  (none)")
        for log in recent:
            self.stdout.write(
                f"  run_id={log.run_id} feed={log.feed_id} status={log.status} finished_at={log.finished_at}"
            )

        self.stdout.write("\n=== Unack Events by severity ===")
        rows = (
            IntegrationEvent.objects.filter(acknowledged_at__isnull=True)
            .values("severity")
            .annotate(count=Count("id"))
            .order_by("severity")
        )
        if not rows:
            self.stdout.write("  (none)")
        for row in rows:
            self.stdout.write(f"  {row['severity']}: {row['count']}")
        # Always show all severities even if zero, to make the contract obvious.
        present = {row["severity"] for row in rows}
        for severity in (EventSeverity.CRITICAL.value, EventSeverity.WARNING.value, EventSeverity.INFO.value):
            if severity not in present:
                self.stdout.write(f"  {severity}: 0")
