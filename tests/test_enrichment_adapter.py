# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the django-enrichment adapter (plan 06).

django-lookup is a soft dependency and is NOT installed in this suite, so its four touchpoints
(`_check_against_pim`, `_default_min_score`, `_rejected_pairs`, `_record_verdict`) are faked — the
adapter's own logic (paging, filtering, gap payload, link, undo) is exercised for real against the
database.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.enums import EventType, ProductStatus
from django_atlas.models import IntegrationEvent
from django_atlas.services import enrichment_adapter
from tests.factories import SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db

MATCH = enrichment_adapter.DECISION_MATCH
REVIEW = enrichment_adapter.DECISION_REVIEW


@pytest.fixture
def source():
    return SourceFactory(idx="acme")


@pytest.fixture
def real_product():
    return RealProduct.objects.create(sku="RP-1", ean="5901234123457", weight=Decimal("0.150"))


def _candidate(sku: str = "RP-1", score: int = 80, decision: str = MATCH) -> dict:
    return {
        "real_product_sku": sku,
        "score": score,
        "decision": decision,
        "reasons": [{"code": "gtin_exact", "label": "Same GTIN", "score": 60, "observed": {}}],
    }


@pytest.fixture
def lookup(monkeypatch):
    """Fake the four django-lookup touchpoints; `results` drives what `check` answers."""
    fake = SimpleNamespace(results=[_candidate()], rejected=set(), verdicts=[], checked=[])

    def check(source_product):
        fake.checked.append(source_product.external_id)
        return fake.results

    monkeypatch.setattr(enrichment_adapter, "_check_against_pim", check)
    monkeypatch.setattr(enrichment_adapter, "_default_min_score", lambda: 45)
    monkeypatch.setattr(enrichment_adapter, "_rejected_pairs", lambda refs: fake.rejected)
    monkeypatch.setattr(
        enrichment_adapter,
        "_record_verdict",
        lambda proposal, decision: fake.verdicts.append((proposal.subject_ref, decision)),
    )
    return fake


def _proposal(subject_ref: str = "acme:EXT-1", **overrides) -> SimpleNamespace:
    payload = {
        "subject_ref": subject_ref,
        "target_kind": enrichment_adapter.TARGET_KIND,
        "target_locator": {"source_idx": "acme", "external_id": subject_ref.split(":")[1]},
        "proposed_value": _candidate(),
        "reviewed_by": None,
    }
    return SimpleNamespace(**{**payload, **overrides})


def test_resolve_targets_lists_only_unlinked_products(source, real_product):
    SourceProductFactory(source=source, external_id="EXT-1")
    SourceProductFactory(source=source, external_id="EXT-2", real_product=real_product)
    SourceProductFactory(source=source, external_id="EXT-3", status=ProductStatus.REJECTED.value)

    assert enrichment_adapter.resolve_targets({"mode": "filter", "filters": {}}) == ["acme:EXT-1"]


def test_resolve_targets_filters_by_source_and_status(source):
    other = SourceFactory(idx="other")
    SourceProductFactory(source=source, external_id="EXT-1", status=ProductStatus.NEW.value)
    SourceProductFactory(source=source, external_id="EXT-2", status=ProductStatus.APPROVED.value)
    SourceProductFactory(source=other, external_id="EXT-3", status=ProductStatus.NEW.value)

    scope = {"mode": "filter", "filters": {"source_idx": "acme", "status": ProductStatus.NEW.value}}

    assert enrichment_adapter.resolve_targets(scope) == ["acme:EXT-1"]


def test_resolve_targets_pages(source, monkeypatch):
    monkeypatch.setattr(enrichment_adapter, "_PAGE_SIZE", 2)
    for index in range(3):
        SourceProductFactory(source=source, external_id=f"EXT-{index}")

    assert enrichment_adapter.resolve_targets({"mode": "filter"}, page=1) == ["acme:EXT-0", "acme:EXT-1"]
    assert enrichment_adapter.resolve_targets({"mode": "filter"}, page=2) == ["acme:EXT-2"]


def test_find_gaps_rejects_an_unknown_check_key(source, lookup):
    with pytest.raises(ValueError, match="unknown atlas check key"):
        enrichment_adapter.find_gaps("no_such_check", {}, {})


def test_find_gaps_emits_the_full_proposal_payload(source, lookup):
    source_product = SourceProductFactory(source=source, external_id="EXT-1", name="Bosch GSR 12V-35")

    gaps = enrichment_adapter.find_gaps(enrichment_adapter.CHECK_DUPLICATE_IN_PIM, {}, {})

    assert gaps == [
        {
            "target_module": "atlas",
            "target_type": "source_product",
            "subject_ref": "acme:EXT-1",
            "subject_label": "Bosch GSR 12V-35",
            "subject_url": f"/api/atlas/v2/admin/products/{source_product.pk}/",
            "target_kind": "link_to_realproduct",
            "target_locator": {"source_idx": "acme", "external_id": "EXT-1"},
            "proposed_value": _candidate(),
            "confidence": 0.8,
        }
    ]


@pytest.mark.parametrize(
    ("decision", "score", "expected"),
    [(MATCH, 80, 1), (REVIEW, 50, 1), ("no_match", 30, 0), (REVIEW, 44, 0)],
)
def test_find_gaps_proposes_only_match_or_review_above_the_threshold(source, lookup, decision, score, expected):
    SourceProductFactory(source=source, external_id="EXT-1")
    lookup.results = [_candidate(score=score, decision=decision)]

    gaps = enrichment_adapter.find_gaps(enrichment_adapter.CHECK_DUPLICATE_IN_PIM, {}, {})

    assert len(gaps) == expected


def test_find_gaps_honours_the_min_score_param(source, lookup):
    SourceProductFactory(source=source, external_id="EXT-1")
    lookup.results = [_candidate(score=60, decision=REVIEW)]

    assert enrichment_adapter.find_gaps(enrichment_adapter.CHECK_DUPLICATE_IN_PIM, {"min_score": 70}, {}) == []


def test_find_gaps_skips_a_pair_the_operator_already_rejected(source, lookup):
    SourceProductFactory(source=source, external_id="EXT-1")
    lookup.results = [_candidate(sku="RP-1"), _candidate(sku="RP-2", score=70, decision=REVIEW)]
    lookup.rejected = {("acme:EXT-1", "RP-1")}

    gaps = enrichment_adapter.find_gaps(enrichment_adapter.CHECK_DUPLICATE_IN_PIM, {}, {})

    assert [gap["proposed_value"]["real_product_sku"] for gap in gaps] == ["RP-2"]


def test_find_gaps_pages_and_scopes_the_targets_it_checks(source, lookup, monkeypatch):
    monkeypatch.setattr(enrichment_adapter, "_PAGE_SIZE", 1)
    SourceProductFactory(source=source, external_id="EXT-1")
    SourceProductFactory(source=source, external_id="EXT-2")

    enrichment_adapter.find_gaps(enrichment_adapter.CHECK_DUPLICATE_IN_PIM, {}, {"page": 2})

    assert lookup.checked == ["EXT-2"]


def test_read_current_reports_the_live_link(source, real_product):
    SourceProductFactory(source=source, external_id="EXT-1")
    linked = SourceProductFactory(source=source, external_id="EXT-2", real_product=real_product)

    unlinked_snapshot = enrichment_adapter.read_current(
        subject_ref="acme:EXT-1", target_kind=enrichment_adapter.TARGET_KIND, target_locator={}
    )
    linked_snapshot = enrichment_adapter.read_current(
        subject_ref="acme:EXT-2",
        target_kind=enrichment_adapter.TARGET_KIND,
        target_locator={"source_idx": "acme", "external_id": linked.external_id},
    )

    assert unlinked_snapshot == {"real_product_sku": None}
    assert linked_snapshot == {"real_product_sku": "RP-1"}


def test_read_current_raises_when_the_source_product_is_gone(source):
    from django_atlas.models import SourceProduct

    with pytest.raises(SourceProduct.DoesNotExist):
        enrichment_adapter.read_current(
            subject_ref="acme:EXT-404", target_kind=enrichment_adapter.TARGET_KIND, target_locator={}
        )


def test_read_current_rejects_a_foreign_target_kind(source):
    with pytest.raises(ValueError, match="unsupported target_kind"):
        enrichment_adapter.read_current(subject_ref="acme:EXT-1", target_kind="picture", target_locator={})


def test_apply_links_the_source_product_and_logs_the_verdict(source, real_product, lookup):
    source_product = SourceProductFactory(source=source, external_id="EXT-1")

    enrichment_adapter.apply(_proposal())

    source_product.refresh_from_db()
    assert source_product.real_product == real_product
    assert lookup.verdicts == [("acme:EXT-1", enrichment_adapter.DECISION_ACCEPTED)]
    event = IntegrationEvent.objects.get(event_type=EventType.LINKED_VIA_LOOKUP_PROPOSAL.value)
    assert event.source_product_id == source_product.id
    assert event.details["real_product_sku"] == "RP-1"


def test_apply_fails_loud_when_the_sku_no_longer_exists(source, lookup):
    source_product = SourceProductFactory(source=source, external_id="EXT-1")

    with pytest.raises(ValueError, match="does not exist"):
        enrichment_adapter.apply(_proposal())

    source_product.refresh_from_db()
    assert source_product.real_product is None
    assert lookup.verdicts == []


def test_apply_requires_a_sku_in_the_proposed_value(source, lookup):
    SourceProductFactory(source=source, external_id="EXT-1")

    with pytest.raises(ValueError, match="requires a 'real_product_sku'"):
        enrichment_adapter.apply(_proposal(proposed_value={"score": 80}))


def test_revert_unlinks_what_apply_linked(source, real_product, lookup):
    source_product = SourceProductFactory(source=source, external_id="EXT-1", real_product=real_product)

    enrichment_adapter.revert(_proposal())

    source_product.refresh_from_db()
    assert source_product.real_product is None


def test_revert_leaves_a_manual_relink_alone(source, real_product, lookup):
    other = RealProduct.objects.create(sku="RP-OTHER", weight=Decimal("0.200"))
    source_product = SourceProductFactory(source=source, external_id="EXT-1", real_product=other)

    enrichment_adapter.revert(_proposal())

    source_product.refresh_from_db()
    assert source_product.real_product == other


def test_on_reject_records_the_operators_no(source, lookup):
    enrichment_adapter.on_reject(_proposal())

    assert lookup.verdicts == [("acme:EXT-1", enrichment_adapter.DECISION_REJECTED)]


def test_on_reject_ignores_a_foreign_target_kind(source, lookup):
    enrichment_adapter.on_reject(_proposal(target_kind="picture"))

    assert lookup.verdicts == []


def test_check_against_pim_skips_a_row_without_any_identifier(source):
    """The guard runs before django-lookup is imported — a nameless row costs no query."""
    source_product = SourceProductFactory(source=source, external_id="EXT-1", name="", ean="")

    assert enrichment_adapter._check_against_pim(source_product) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.2", Decimal("1.2")), (0, Decimal("0")), ("", None), (None, None), ("heavy", None), ("-1", None)],
)
def test_feed_values_reach_the_query_only_when_they_are_usable_decimals(raw, expected):
    assert enrichment_adapter._decimal(raw) == expected
