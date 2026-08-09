# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Push approved SourceProducts for a source (sync default; --async via Celery)."""

from django.core.management.base import BaseCommand, CommandError

from django_atlas.models import Source
from django_atlas.services import push_service


class Command(BaseCommand):
    help = "Push every approved SourceProduct for a source."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--source-idx", type=str, required=True)
        parser.add_argument("--async", dest="run_async", action="store_true", default=False)

    def handle(self, *args, **options) -> None:
        source_idx = options["source_idx"]
        try:
            source = Source.objects.get(idx=source_idx)
        except Source.DoesNotExist as exc:
            raise CommandError(f"Source '{source_idx}' not found") from exc

        if options["run_async"]:
            from django_atlas.tasks.push_pipeline import push_approved_for_source_task

            result = push_approved_for_source_task.delay(source_id=source.id, user_id=None)
            self.stdout.write(f"Dispatched async: task_id={result.id}, source='{source_idx}'")
            return

        try:
            counts = push_service.push_approved_for_source(source.id, user=None)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"source='{source_idx}' success={counts.get('success', 0)} failed={counts.get('failed', 0)} "
            f"preflight_failed={counts.get('preflight_failed', False)}"
        )
