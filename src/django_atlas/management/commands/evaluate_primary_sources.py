# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Run the auto-primary selection batch synchronously.

Default entry point for the operator playbook: `python manage.py evaluate_primary_sources`
runs the same logic as the celery beat task, but blocks and prints the summary
to stdout. Use this when:
  - the celery-beat container is down and you want to recover storefront pricing now
  - debugging why a particular SKU's switch did not fire (combine with --sku)
  - manual override → reset-to-auto flow is being re-tested in E2E

`--sku SKU` evaluates a single RealProduct (still respects safety guards unless
`--bypass-safety` is also passed — same semantics as the inline emergency trigger).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from django_atlas.services import primary_strategy_service
from django_atlas.tasks.primary_strategy import evaluate_all


class Command(BaseCommand):
    help = "Evaluate auto-primary sources for every multi-source RealProduct."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--sku",
            type=str,
            default=None,
            help="Evaluate a single RealProduct (by sku) instead of every multi-source RP.",
        )
        parser.add_argument(
            "--bypass-safety", action="store_true", help="Skip cooldown + hysteresis guards (emergency-mode semantics)."
        )

    def handle(self, *args, **options) -> None:
        sku = options.get("sku")
        bypass = bool(options.get("bypass_safety"))
        try:
            if sku:
                result = primary_strategy_service.evaluate_and_apply(sku, bypass_safety=bypass)
                line = (
                    f"sku={sku} switched={result.should_switch} "
                    f"skip_reason={result.skip_reason.value} "
                    f"winner={result.new_primary.source.idx if result.new_primary else None}"
                )
                self.stdout.write(line)
                return
            summary = evaluate_all(bypass_safety=bypass)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc
        self.stdout.write("Auto-primary evaluation summary:")
        for key, value in summary.items():
            self.stdout.write(f"  {key}: {value}")
