# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Performance gate for the bulk has-changes endpoint.

Constraint: 100 SKU lookup must hit DB at most 3 times (query-count test —
the real regression guard for N+1 / dropped FILTER annotation on the
`atlas_changelog_sku_unseen_idx (real_product_sku, applied_to_pim, -created_at)`
index) and complete within a wall-clock budget. The timing test is a smoke
check with slack for a loaded CI/gate host, not a precise benchmark — it
measures best-of-3 to absorb host-noise spikes.
"""

import os
import time

import pytest

from django_atlas.enums import ChangeLogSource
from django_atlas.models import SourceProductChangeLog, SourceProductLink
from django_atlas.services import change_log_service
from tests.factories import SourceProductFactory

pytestmark = pytest.mark.django_db


def _seed_bulk(skus: list[str], rows_per_sku: int) -> None:
    sp = SourceProductFactory()
    SourceProductLink.objects.bulk_create(
        [SourceProductLink(real_product_sku=sku, source=sp.source, is_active=True, is_primary=False) for sku in skus]
    )
    log_rows: list[SourceProductChangeLog] = []
    for sku in skus:
        for i in range(rows_per_sku):
            log_rows.append(
                SourceProductChangeLog(
                    source_product=sp,
                    real_product_sku=sku,
                    source=ChangeLogSource.DELTA_SYNC.value,
                    field_path=f"field-{i}",
                    before=i,
                    after=i + 1,
                    applied_to_pim=(i % 2 == 0),
                )
            )
    SourceProductChangeLog.objects.bulk_create(log_rows, batch_size=500)


def test_bulk_has_changes_100_skus_few_queries(django_assert_max_num_queries):
    skus = [f"PERF-{i:04d}" for i in range(100)]
    _seed_bulk(skus, rows_per_sku=10)
    with django_assert_max_num_queries(3):
        result = change_log_service.bulk_has_changes(skus)
    assert len(result) == 100


def test_bulk_has_changes_100_skus_under_budget():
    skus = [f"PERF-{i:04d}" for i in range(100)]
    _seed_bulk(skus, rows_per_sku=10)
    budget_ms = int(os.environ.get("ATLAS_PERF_BUDGET_MS", "300"))

    best_elapsed = min(_time_bulk_has_changes(skus) for _ in range(3))

    assert best_elapsed < budget_ms / 1000, (
        f"bulk_has_changes(100) best-of-3 took {best_elapsed * 1000:.1f}ms (>{budget_ms}ms)"
    )


def _time_bulk_has_changes(skus: list[str]) -> float:
    start = time.perf_counter()
    result = change_log_service.bulk_has_changes(skus)
    elapsed = time.perf_counter() - start
    assert len(result) == 100
    return elapsed
