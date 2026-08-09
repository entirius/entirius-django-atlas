# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models
from django.utils import timezone
from django_utils.models.base_model import BaseModel

from django_atlas.enums import SourceKind


class Observation(BaseModel):
    """Append-only external-data observation log.

    Written once per (source, sku, ts) — services expose no update/delete path.
    The retention beat (`tasks/retention.py`) is the only bulk-delete path.

    Canonical `value` shape depends on `kind` (denormalized from `source.kind`
    at write time so read queries don't need a join back to Source):
      - monitoring -> {"price": <str/decimal>, "currency": <str>, "stock": <int|null>}
      - enrichment -> {"signals": {...arbitrary...}}
      - procurement sources do not currently write Observations — cost flows via
        `cost_updated_signal` / `pricemanager_writer` instead. Not hard-blocked at
        the model level; a procurement Observation is simply unused today.

    Market context (country/currency) is intentionally NOT duplicated here — read
    it from `source.country` / `source.currency` at query time so it can never
    drift from the Source's actual configuration.

    `ts` is the semantically meaningful timestamp for querying/ordering. It
    defaults to `timezone.now` but is NOT `auto_now_add`, so batch/backfill
    writers can set a custom timestamp. `created_at`/`modified_at` (from
    `BaseModel`) are plain bookkeeping, not query-relevant.
    """

    source = models.ForeignKey("django_atlas.Source", on_delete=models.CASCADE, related_name="observations")
    sku = models.CharField(max_length=128, db_index=True)
    kind = models.CharField(max_length=20, choices=SourceKind.choices)
    value = models.JSONField()
    ts = models.DateTimeField(default=timezone.now)
    raw = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["sku", "kind", "source", "-ts"], name="atlas_observation_lookup_idx"),
            models.Index(fields=["kind", "-ts"], name="atlas_observation_kind_ts_idx"),
        ]

    def save(self, *args: object, **kwargs: object) -> None:
        """Append-only: block mutating a row already loaded/persisted from the DB."""
        if not self._state.adding:
            raise ValueError("Observation is append-only — updates are not allowed")
        kwargs.setdefault("force_insert", True)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Observation({self.sku}, {self.kind}, source={self.source_id}, ts={self.ts})"
