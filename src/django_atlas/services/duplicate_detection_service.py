# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Duplicate RealProduct detection.

Reusable service shared between the `find_duplicate_realproducts` management
command and the `GET /api/atlas/v2/admin/duplicates/` admin endpoint. Read-only.

Returns EAN groups that contain >1 RealProduct, each annotated with a per-group
suggestion (`merge` / `review`) based on the largest pairwise weight diff vs the
configurable tolerance. Conservative: any missing weight in a group bumps the
suggestion to `review` (no auto-guess).

The dataclasses below are intentionally plain (`@dataclass(frozen=True)`) and
contain primitives only -- the HTTP endpoint converts them to Pydantic for the
response, and the CLI command formats them as plain text. Neither touches ORM
after the service returns.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class RPSnapshot:
    sku: str
    ean: str
    weight: Decimal | None
    width: Decimal | None
    height: Decimal | None
    deep: Decimal | None
    sources: tuple[dict, ...]  # ({idx, name, is_primary}, ...)


@dataclass(frozen=True)
class DuplicateGroup:
    ean: str
    realproducts: tuple[RPSnapshot, ...]
    suggestion: Literal["merge", "review"]
    suggestion_detail: str


_DIFF_PCT_FLOOR = Decimal("0.001")


def find_duplicates_by_ean(*, tolerance_pct: float = 10.0) -> list[DuplicateGroup]:
    """Return all EAN groups with >1 RealProduct, annotated with merge suggestion.

    `tolerance_pct` controls when the per-group suggestion flips from `merge`
    (max pairwise weight diff <= tolerance) to `review` (above tolerance, or
    any RealProduct missing weight).

    Ordering: groups sorted by EAN ascending, RealProducts within a group sorted
    by SKU ascending so the output is deterministic across calls.
    """
    from django.db.models import Count
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import SourceProductLink

    tolerance = Decimal(str(tolerance_pct))

    ean_groups = (
        RealProduct.objects.exclude(ean__isnull=True)
        .exclude(ean__exact="")
        .values("ean")
        .annotate(group_size=Count("id"))
        .filter(group_size__gt=1)
        .order_by("ean")
    )

    eans = [row["ean"] for row in ean_groups]
    if not eans:
        return []

    rps_by_ean: dict[str, list[RealProduct]] = {}
    for rp in RealProduct.objects.filter(ean__in=eans).order_by("ean", "sku"):
        rps_by_ean.setdefault(rp.ean, []).append(rp)

    skus = [rp.sku for rps in rps_by_ean.values() for rp in rps]
    links_by_sku: dict[str, list[dict]] = {}
    for link in SourceProductLink.objects.filter(real_product_sku__in=skus).select_related("source"):
        links_by_sku.setdefault(link.real_product_sku, []).append(
            {"idx": link.source.idx, "name": link.source.name, "is_primary": bool(link.is_primary)}
        )

    out: list[DuplicateGroup] = []
    for ean in eans:
        rps = rps_by_ean.get(ean, [])
        snapshots = tuple(
            RPSnapshot(
                sku=rp.sku,
                ean=rp.ean,
                weight=rp.weight,
                width=rp.width,
                height=rp.height,
                deep=rp.deep,
                sources=tuple(links_by_sku.get(rp.sku, [])),
            )
            for rp in rps
        )
        suggestion, detail = _suggest_action(rps, tolerance)
        out.append(DuplicateGroup(ean=ean, realproducts=snapshots, suggestion=suggestion, suggestion_detail=detail))
    return out


def _suggest_action(rps: list, tolerance: Decimal) -> tuple[Literal["merge", "review"], str]:
    """Return ('merge'|'review', human-readable detail) for a single EAN group.

    Mirrors the management-command heuristic introduced with auto EAN-match: missing
    weight -> review, otherwise the max pairwise diff decides.
    """
    weights = [rp.weight for rp in rps]
    if any(w is None for w in weights):
        return "review", "Missing weight on at least one RealProduct"
    max_diff_pct = Decimal("0")
    for i, w_i in enumerate(weights):
        for w_j in weights[i + 1 :]:
            denom = max(abs(w_i), abs(w_j), _DIFF_PCT_FLOOR)
            diff_pct = (abs(w_i - w_j) / denom) * Decimal("100")
            if diff_pct > max_diff_pct:
                max_diff_pct = diff_pct
    if max_diff_pct <= tolerance:
        return "merge", f"Max weight diff {max_diff_pct:.1f}% within {tolerance:.0f}% tolerance"
    return "review", f"Max weight diff {max_diff_pct:.1f}% exceeds {tolerance:.0f}% tolerance"
