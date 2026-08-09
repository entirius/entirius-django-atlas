# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run a SourceFeed (sync mode by default; --async dispatches Celery task)."""

from django.core.management.base import BaseCommand, CommandError

from django_atlas.models import SourceFeed
from django_atlas.services import feed_service, import_service


class Command(BaseCommand):
    help = "Execute a source feed (sync or async via Celery)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--feed-id", type=int, default=None)
        parser.add_argument("--source-idx", type=str, default=None)
        parser.add_argument("--feed-idx", type=str, default=None)
        parser.add_argument("--mode", choices=["full", "delta"], default="full")
        parser.add_argument("--async", dest="run_async", action="store_true", default=False)

    def handle(self, *args, **options) -> None:
        feed = self._resolve_feed(options)
        mode = options["mode"]
        if options["run_async"]:
            from django_atlas.tasks.feed_execution import execute_feed_task

            result = execute_feed_task.delay(feed.id, mode=mode)
            self.stdout.write(f"Dispatched async: task_id={result.id}, feed_id={feed.id}, mode={mode}")
            return

        try:
            log = import_service.execute_feed(feed, mode=mode, source="cli", triggered_by=None)
        except Exception as exc:  # noqa: BLE001 — surface clean CLI error
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"run_id={log.run_id} status={log.status} new={log.new_count} updated={log.updated_count} "
            f"delisted={log.delisted_count} errors={log.error_count}"
        )

    def _resolve_feed(self, options: dict) -> SourceFeed:
        feed_id = options["feed_id"]
        source_idx = options["source_idx"]
        feed_idx = options["feed_idx"]
        if feed_id is not None:
            try:
                return SourceFeed.objects.get(pk=feed_id)
            except SourceFeed.DoesNotExist as exc:
                raise CommandError(f"Feed id={feed_id} not found") from exc
        if source_idx and feed_idx:
            try:
                return feed_service.get_feed(source_idx, feed_idx)
            except SourceFeed.DoesNotExist as exc:
                raise CommandError(f"Feed {source_idx}/{feed_idx} not found") from exc
        raise CommandError("Provide --feed-id N or both --source-idx X and --feed-idx Y")
