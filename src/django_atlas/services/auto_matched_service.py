# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cross-source dashboard: list RealProducts touched by the auto-EAN-match path.

Powers `GET /api/atlas/v2/admin/auto-matched/`. Returns one row
per RealProduct SKU that appears in `SourceProductChangeLog.source=auto_link`
at least once, joined with current `SourceProductLink` rows + a couple of flags
the operator wants to filter on.

The dedupe step uses an aggregating `values().annotate(Max(...))` so the row
count is "unique SKUs" rather than "audit rows", which is what the UI shows.
Pagination is applied on the deduped SKU list, not the audit table.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Max, QuerySet

from django_atlas.enums import ChangeLogSource, EventType


@dataclass(frozen=True)
class AutoMatchedSource:
    idx: str
    name: str
    is_primary: bool


@dataclass(frozen=True)
class AutoMatchedRow:
    sku: str
    ean: str | None
    sources: tuple[AutoMatchedSource, ...]
    has_tolerance_violation: bool
    has_manual_override: bool
    last_auto_link_at: datetime


def list_distinct_sku_qs(
    *, source_idx: str | None = None, since: datetime | None = None, manual_override_only: bool = False
) -> QuerySet:
    """Return a non-materialised queryset of {real_product_sku, last_at} dicts.

    Caller paginates this queryset. The order is `-last_at` so the most recently
    auto-linked SKUs surface first (helpful for operators triaging fresh runs).

    `source_idx` filters audit rows to those whose SP belongs to that source;
    `since` truncates by audit-row timestamp; `manual_override_only` narrows the
    SKU set to those where ANY active SourceProductLink has manual_override=True.
    """
    from django_atlas.models import SourceProductChangeLog, SourceProductLink

    qs = SourceProductChangeLog.objects.filter(source=ChangeLogSource.AUTO_LINK.value).exclude(
        real_product_sku__exact=""
    )
    if source_idx:
        qs = qs.filter(source_product__source__idx=source_idx)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    deduped = qs.values("real_product_sku").annotate(last_at=Max("created_at")).order_by("-last_at")
    if manual_override_only:
        override_skus = SourceProductLink.objects.filter(manual_override=True).values_list(
            "real_product_sku", flat=True
        )
        deduped = deduped.filter(real_product_sku__in=list(override_skus))
    return deduped


def build_rows(distinct_rows: list[dict], *, has_violations_only: bool = False) -> list[AutoMatchedRow]:
    """Hydrate the lightweight `[{real_product_sku, last_at}, ...]` page into full rows.

    Single batch query per related table (RealProduct, SourceProductLink, IntegrationEvent)
    — N=page_size, no N+1.
    """
    from django_pim.models.real_product import RealProduct

    from django_atlas.models import IntegrationEvent, SourceProductLink

    skus = [row["real_product_sku"] for row in distinct_rows]
    if not skus:
        return []

    ean_by_sku: dict[str, str | None] = {
        rp.sku: (rp.ean if rp.ean else None) for rp in RealProduct.objects.filter(sku__in=skus)
    }

    links_by_sku: dict[str, list[AutoMatchedSource]] = {}
    override_skus: set[str] = set()
    for link in SourceProductLink.objects.filter(real_product_sku__in=skus, is_active=True).select_related("source"):
        links_by_sku.setdefault(link.real_product_sku, []).append(
            AutoMatchedSource(idx=link.source.idx, name=link.source.name, is_primary=bool(link.is_primary))
        )
        if link.manual_override:
            override_skus.add(link.real_product_sku)

    violation_skus: set[str] = set(
        IntegrationEvent.objects.filter(
            event_type=EventType.PHYSICAL_TOLERANCE_VIOLATION.value, source_product__real_product__sku__in=skus
        ).values_list("source_product__real_product__sku", flat=True)
    )

    out: list[AutoMatchedRow] = []
    for row in distinct_rows:
        sku = row["real_product_sku"]
        has_violation = sku in violation_skus
        if has_violations_only and not has_violation:
            continue
        out.append(
            AutoMatchedRow(
                sku=sku,
                ean=ean_by_sku.get(sku),
                sources=tuple(links_by_sku.get(sku, [])),
                has_tolerance_violation=has_violation,
                has_manual_override=sku in override_skus,
                last_auto_link_at=row["last_at"],
            )
        )
    return out
