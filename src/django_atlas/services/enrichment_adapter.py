# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Atlas adapter for the django-enrichment bus — the `duplicate_in_pim` acceptance queue.

The bus never imports atlas: it loads this module lazily by dotted path from
`settings.ENRICHMENT_ADAPTERS = {"atlas": "django_atlas.services.enrichment_adapter"}` and calls the
module-level functions below (duck-typed against `django_enrichment.adapters.base`). Nothing from
django-enrichment is imported here — the dependency points one way.

One check, one target kind: an unlinked `SourceProduct` that looks like a product PIM already has.
`find_gaps` asks django-lookup ("is this a duplicate?"), the operator answers in the review queue,
`apply` writes the link. v1 is **always a proposal** (decision #3) — nothing is linked automatically;
`Source.auto_accept_min_score` is the seam a later version uses, unused here.

Atlas locator / value convention (opaque to the bus — only this module reads them):

- `subject_ref`     = `<source.idx>:<external_id>` (the lookup provider's ref, stable across imports)
- `target_kind`     = `"link_to_realproduct"`
- `target_locator`  = `{"source_idx": <idx>, "external_id": <id>}`
- `proposed_value`  = `{"real_product_sku", "score", "decision", "reasons"}` (one lookup candidate)
- `current_snapshot`= `{"real_product_sku": <sku currently linked, or None>}`

django-lookup is a **soft** dependency: every import of it sits inside a function (atlas must boot,
and its own test suite must run, without the module installed). Those functions are also the seam
the adapter tests fake.

Coexistence (decision #1): the EAN auto-link in `pim_writer.init_push_to_channel` is untouched —
this path only advises. A source product it already linked leaves the candidate pool by itself.
"""

from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from django_pim.models.real_product import RealProduct

from django_atlas.enums import EventSeverity, EventType
from django_atlas.models import SourceProduct
from django_atlas.services import event_service, lookup_provider

MODULE = "atlas"
CHECK_DUPLICATE_IN_PIM = "duplicate_in_pim"
TARGET_KIND = "link_to_realproduct"
TARGET_TYPE = "source_product"
# Mirrors of django_lookup's enums (never imported — soft dependency, same rule as lookup_provider).
PIM_KIND = "pim_product"
DECISION_MATCH = "match"
DECISION_REVIEW = "review"
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"

# Only a verdict worth an operator's time becomes a proposal; `no_match` never does.
_PROPOSABLE = (DECISION_MATCH, DECISION_REVIEW)
# How many source products one `find_gaps` page checks. Each one is a lookup query, so this page is
# far smaller than PIM's — `SpawnRule.limit` bounds the run, this bounds one pull.
_PAGE_SIZE = 25
# Candidates asked of lookup per source product: the operator sees one, the rest are the fallback
# when the top candidate was already rejected for this subject.
_CANDIDATE_LIMIT = 3
_PHYSICAL_ATTRS = ("weight", "width", "height", "deep")


def resolve_targets(scope_spec: dict, page: int = 1) -> list[str]:
    """Page of refs of source products still waiting for a RealProduct (`filter` / `list` scope)."""
    offset = (max(page, 1) - 1) * _PAGE_SIZE
    if scope_spec.get("mode") in ("list", "csv"):
        return list((scope_spec.get("refs") or [])[offset : offset + _PAGE_SIZE])
    queryset = _queryset(scope_spec.get("filters") or {})
    return [lookup_provider.ref_for(row) for row in queryset[offset : offset + _PAGE_SIZE]]


def find_gaps(check: str, params: dict, scope: dict) -> list[dict]:
    """Page of duplicate candidates: one `lookup.check` per unlinked source product.

    `params` may carry `min_score` (default: lookup's `review` threshold). `scope` carries the
    filters (`source_idx`, `status`) plus `page`, injected by the bus. The bus does not validate
    `check_key`, so an unknown one must fail loud here (`ValueError` → 400 on the run-rule call).
    """
    if check != CHECK_DUPLICATE_IN_PIM:
        raise ValueError(f"unknown atlas check key {check!r} (known: {CHECK_DUPLICATE_IN_PIM!r})")
    page = max(int(scope.get("page", 1)), 1)
    offset = (page - 1) * _PAGE_SIZE
    targets = list(_queryset(scope)[offset : offset + _PAGE_SIZE])
    min_score = int(params.get("min_score") or _default_min_score())
    rejected = _rejected_pairs([lookup_provider.ref_for(row) for row in targets])
    gaps = (_gap(row, min_score, rejected) for row in targets)
    return [gap for gap in gaps if gap is not None]


def read_current(*, subject_ref: str, target_kind: str, target_locator: dict) -> dict:
    """The link the source product carries right now — the bus's drift and undo anchor."""
    if target_kind != TARGET_KIND:
        raise ValueError(f"unsupported target_kind {target_kind!r} for the atlas adapter")
    source_product = _resolve(subject_ref, target_locator)
    real_product = source_product.real_product
    return {"real_product_sku": real_product.sku if real_product is not None else None}


def apply(proposal) -> None:
    """Link the source product to the proposed RealProduct — the only write back into atlas.

    Runs inside the bus's transaction (no Celery, no HTTP). Drift is already checked; a SKU that
    no longer exists is an operator situation, so it raises `ValueError` (→ 400) and the proposal
    stays pending instead of being marked applied.
    """
    value = proposal.proposed_value or {}
    sku = value.get("real_product_sku")
    if not sku:
        raise ValueError("link_to_realproduct apply requires a 'real_product_sku'")
    # `sku__iexact`: RealProduct SKUs are unique case-insensitively and the link service resolves them
    # the same way — an exact filter here would accept a proposal the UI link path resolves fine.
    real_product = RealProduct.objects.filter(sku__iexact=sku).first()
    if real_product is None:
        raise ValueError(f"RealProduct {sku!r} does not exist — {proposal.subject_ref!r} was not linked")
    source_product = _resolve(proposal.subject_ref, proposal.target_locator)
    source_product.real_product = real_product
    source_product.save(update_fields=["real_product", "modified_at"])
    _emit_link_event(source_product, value)
    _record_verdict(proposal, DECISION_ACCEPTED)


def revert(proposal) -> None:
    """Undo the link — but only while it is still the one we wrote (a manual relink wins)."""
    sku = (proposal.proposed_value or {}).get("real_product_sku")
    source_product = _resolve(proposal.subject_ref, proposal.target_locator)
    linked = source_product.real_product
    if linked is None or linked.sku != sku:
        return
    source_product.real_product = None
    source_product.save(update_fields=["real_product", "modified_at"])


def on_reject(proposal) -> None:
    """Optional bus hook: remember "not the same product" so the pair is never proposed again.

    The bus's own reject cooldown expires; this verdict does not (`django_lookup.dedup_log`).
    """
    if proposal.target_kind != TARGET_KIND:
        return
    _record_verdict(proposal, DECISION_REJECTED)


def _queryset(filters: dict):
    """The candidate universe: unlinked, not rejected, stable order (the bus re-pulls page 1)."""
    queryset = lookup_provider.candidates()
    if source_idx := filters.get("source_idx"):
        queryset = queryset.filter(source__idx=source_idx)
    if status := filters.get("status"):
        queryset = queryset.filter(status=status)
    return queryset.select_related("source__default_language").order_by("id")


def _gap(source_product: SourceProduct, min_score: int, rejected: set[tuple[str, str]]) -> dict | None:
    """One source product → one gap candidate, or None when nothing is worth an operator's time."""
    ref = lookup_provider.ref_for(source_product)
    candidate = _best_candidate(ref, _check_against_pim(source_product), min_score, rejected)
    if candidate is None:
        return None
    return {
        "target_module": MODULE,
        "target_type": TARGET_TYPE,
        "subject_ref": ref,
        "subject_label": source_product.name,
        "subject_url": lookup_provider.DETAIL_URL.format(pk=source_product.pk),
        "target_kind": TARGET_KIND,
        "target_locator": _locator(ref),
        "proposed_value": candidate,
        # ContentProposal.confidence is a 0-1 Decimal(4,3); the score is 0-100.
        "confidence": round(candidate["score"] / 100, 3),
    }


def _best_candidate(ref: str, candidates: list[dict], min_score: int, rejected: set[tuple[str, str]]) -> dict | None:
    """First candidate (lookup returns them best-first) the operator has not already answered."""
    for candidate in candidates:
        if candidate["decision"] not in _PROPOSABLE or candidate["score"] < min_score:
            continue
        if (ref, candidate["real_product_sku"]) in rejected:
            continue
        return candidate
    return None


def _locator(ref: str) -> dict:
    source_idx, _, external_id = ref.partition(lookup_provider.REF_SEPARATOR)
    return {"source_idx": source_idx, "external_id": external_id}


def _resolve(subject_ref: str, target_locator: dict) -> SourceProduct:
    """Locate the source product, linked or not (`lookup_provider.candidates()` drops linked rows).

    Raises `SourceProduct.DoesNotExist` — an `ObjectDoesNotExist` is what the bus turns into a
    readable "the target is gone" error instead of a 500.
    """
    locator = target_locator or _locator(subject_ref)
    source_product = (
        SourceProduct.objects.filter(source__idx=locator.get("source_idx"), external_id=locator.get("external_id"))
        .select_related("real_product", "source")
        .first()
    )
    if source_product is None:
        raise SourceProduct.DoesNotExist(f"no SourceProduct for {subject_ref!r}")
    return source_product


def _emit_link_event(source_product: SourceProduct, value: dict) -> None:
    """INFO audit trail: this link came from a lookup proposal an operator accepted.

    Deliberately not best-effort like `pim_writer`'s events: we are inside the bus's transaction,
    where swallowing a database error would be worse than failing the apply.
    """
    sku = value.get("real_product_sku")
    event_service.record(
        event_type=EventType.LINKED_VIA_LOOKUP_PROPOSAL.value,
        severity=EventSeverity.INFO.value,
        source=source_product.source,
        source_product=source_product,
        message=f"Linked SP {source_product.id} to RealProduct {sku} via an accepted lookup proposal",
        details={
            "real_product_sku": sku,
            "score": value.get("score"),
            "decision": value.get("decision"),
            "reasons": [reason.get("code") for reason in value.get("reasons") or []],
        },
    )


def _check_against_pim(source_product: SourceProduct) -> list[dict]:
    """`lookup.check` for one source product → candidates already in `proposed_value` shape.

    One of the four django-lookup touchpoints (lazy import, faked by the adapter tests).
    """
    item = lookup_provider.get_item(lookup_provider.ref_for(source_product))
    if not any((item.gtin, item.mpn, item.name_by_lang)):
        return []  # a feed row with no identifier at all — nothing to search on (and nothing to import)

    from django_lookup.enums import DecisionSource
    from django_lookup.schemas.requests.lookup import Attrs, LookupQuery
    from django_lookup.services import lookup_service

    query = LookupQuery(
        ean=item.gtin,
        brand=item.brand,
        mpn=item.mpn,
        name=next(iter(item.name_by_lang.values()), None),
        attrs=Attrs(**{name: _decimal(item.attrs.get(name)) for name in _PHYSICAL_ATTRS}),
        scope=[PIM_KIND],
        limit=_CANDIDATE_LIMIT,
    )
    # Tag the log with the caller: a bulk enricher run must stay separable from operator API traffic.
    result = lookup_service.check(query, source=DecisionSource.PROPOSAL)
    return [_candidate(hit) for hit in result.candidates]


def _candidate(hit) -> dict:
    """One lookup hit → the payload the operator reviews and `apply` writes."""
    return {
        "real_product_sku": hit.ref,
        "score": hit.score,
        "decision": hit.decision,
        "reasons": [asdict(reason) for reason in hit.reasons],
    }


def _decimal(raw) -> Decimal | None:
    """A feed's physical value as a decimal; anything unusable is simply absent from the query."""
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def _default_min_score() -> int:
    """Lookup's own `review` threshold — the floor below which a candidate is not worth reviewing."""
    from django_lookup.settings import get_thresholds

    return int(get_thresholds()["review"])


def _rejected_pairs(subject_refs: list[str]) -> set[tuple[str, str]]:
    """`{(subject_ref, sku)}` an operator already rejected — one query for the whole page."""
    from django_lookup.services import dedup_log

    return dedup_log.rejected_pairs(subject_refs)


def _record_verdict(proposal, decision_human: str) -> None:
    """Append the operator's answer to django-lookup's dedup log (calibration + never re-propose)."""
    from django_lookup.services import dedup_log

    value = proposal.proposed_value or {}
    dedup_log.record(
        dedup_log.Verdict(
            subject_ref=proposal.subject_ref,
            candidate_kind=PIM_KIND,
            candidate_ref=value.get("real_product_sku") or "",
            decision_human=decision_human,
            decision_auto=value.get("decision") or "",
            score=int(value.get("score") or 0),
        ),
        user=proposal.reviewed_by,
    )
