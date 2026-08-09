# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the Supplier/Competitor facades.

Thin `kind`-forced projections of the Source collection — `kind` is never accepted from
the client, and a Source of the *other* kind must 404 through the facade even though it
exists under `/sources/{idx}/`. Response shape must omit the other kind's knobs.
"""

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def source_factory(db, language, currency):
    from django_atlas.models import Source

    counter = {"n": 0}

    def _make(idx: str | None = None, kind: str = "procurement", **overrides) -> Source:
        counter["n"] += 1
        return Source.objects.create(
            idx=idx or f"facade-src-{counter['n']}",
            name=overrides.pop("name", f"Facade Source {counter['n']}"),
            kind=kind,
            default_language=language,
            default_currency=currency,
            **overrides,
        )

    return _make


@pytest.fixture
def regular_client(api_client: APIClient, regular_user) -> APIClient:
    api_client.force_authenticate(user=regular_user)
    return api_client


# -----------------------------------------------------------------------------
# Suppliers
# -----------------------------------------------------------------------------


class TestSuppliersAuth:
    URL = "/api/atlas/v2/admin/suppliers/"

    def test_list_unauthorized(self, api_client):
        assert api_client.get(self.URL).status_code == 401

    def test_list_forbidden_for_regular_user(self, regular_client):
        assert regular_client.get(self.URL).status_code == 403


class TestSuppliersList:
    def test_only_returns_procurement_sources(self, admin_client, source_factory):
        source_factory(idx="sup-1", kind="procurement")
        source_factory(idx="comp-1", kind="monitoring")
        resp = admin_client.get("/api/atlas/v2/admin/suppliers/")
        assert resp.status_code == 200
        idxs = {r["idx"] for r in resp.data["results"]}
        assert "sup-1" in idxs
        assert "comp-1" not in idxs


class TestSuppliersCreate:
    def test_kind_is_forced_to_procurement_even_if_client_sends_monitoring(self, admin_client, language, currency):
        payload = {
            "idx": "sup-forced",
            "name": "Forced",
            "default_language_id": language.id,
            "default_currency_id": currency.id,
            "kind": "monitoring",  # must be ignored — SupplierCreateRequest has no `kind` field
        }
        resp = admin_client.post("/api/atlas/v2/admin/suppliers/", payload, format="json")
        assert resp.status_code == 201
        from django_atlas.models import Source

        assert Source.objects.get(idx="sup-forced").kind == "procurement"

    def test_response_excludes_kind_field(self, admin_client, language, currency):
        payload = {
            "idx": "sup-shape",
            "name": "Shape",
            "default_language_id": language.id,
            "default_currency_id": currency.id,
        }
        resp = admin_client.post("/api/atlas/v2/admin/suppliers/", payload, format="json")
        assert "kind" not in resp.data


class TestSuppliersDetail:
    def test_monitoring_source_returns_404_through_supplier_facade(self, admin_client, source_factory):
        source_factory(idx="cross-1", kind="monitoring")
        resp = admin_client.get("/api/atlas/v2/admin/suppliers/cross-1/")
        assert resp.status_code == 404

    def test_procurement_source_retrieves_ok(self, admin_client, source_factory):
        source_factory(idx="sup-ok", kind="procurement", sku_prefix="SUP")
        resp = admin_client.get("/api/atlas/v2/admin/suppliers/sup-ok/")
        assert resp.status_code == 200
        assert resp.data["idx"] == "sup-ok"
        assert resp.data["sku_prefix"] == "SUP"


class TestSuppliersPatchDelete:
    def test_patch_monitoring_source_returns_404(self, admin_client, source_factory):
        source_factory(idx="cross-2", kind="monitoring")
        resp = admin_client.patch("/api/atlas/v2/admin/suppliers/cross-2/", {"name": "X"}, format="json")
        assert resp.status_code == 404

    def test_delete_monitoring_source_returns_404(self, admin_client, source_factory):
        source_factory(idx="cross-3", kind="monitoring")
        resp = admin_client.delete("/api/atlas/v2/admin/suppliers/cross-3/")
        assert resp.status_code == 404

    def test_patch_procurement_source_ok(self, admin_client, source_factory):
        source_factory(idx="sup-patch", kind="procurement")
        resp = admin_client.patch("/api/atlas/v2/admin/suppliers/sup-patch/", {"name": "Renamed"}, format="json")
        assert resp.status_code == 200
        assert resp.data["name"] == "Renamed"


# -----------------------------------------------------------------------------
# Competitors
# -----------------------------------------------------------------------------


class TestCompetitorsAuth:
    URL = "/api/atlas/v2/admin/competitors/"

    def test_list_unauthorized(self, api_client):
        assert api_client.get(self.URL).status_code == 401

    def test_list_forbidden_for_regular_user(self, regular_client):
        assert regular_client.get(self.URL).status_code == 403


class TestCompetitorsList:
    def test_only_returns_monitoring_sources(self, admin_client, source_factory):
        source_factory(idx="comp-2", kind="monitoring")
        source_factory(idx="sup-2", kind="procurement")
        resp = admin_client.get("/api/atlas/v2/admin/competitors/")
        assert resp.status_code == 200
        idxs = {r["idx"] for r in resp.data["results"]}
        assert "comp-2" in idxs
        assert "sup-2" not in idxs


class TestCompetitorsCreate:
    def test_kind_is_forced_to_monitoring_even_if_client_sends_procurement(self, admin_client, language, currency):
        payload = {
            "idx": "comp-forced",
            "name": "Forced",
            "default_language_id": language.id,
            "default_currency_id": currency.id,
            "kind": "procurement",  # must be ignored — CompetitorCreateRequest has no `kind` field
        }
        resp = admin_client.post("/api/atlas/v2/admin/competitors/", payload, format="json")
        assert resp.status_code == 201
        from django_atlas.models import Source

        assert Source.objects.get(idx="comp-forced").kind == "monitoring"

    def test_procurement_only_fields_rejected_by_schema(self, admin_client, language, currency):
        """Competitor request schema has no `sku_prefix` field — sending it must not be
        silently accepted (pydantic ignores unknown extras by default in these schemas,
        so this asserts it is genuinely absent from the persisted Source, not merely that
        the request 400s)."""
        payload = {
            "idx": "comp-noextra",
            "name": "NoExtra",
            "default_language_id": language.id,
            "default_currency_id": currency.id,
            "sku_prefix": "SHOULD-NOT-LAND",
        }
        resp = admin_client.post("/api/atlas/v2/admin/competitors/", payload, format="json")
        assert resp.status_code == 201
        from django_atlas.models import Source

        source = Source.objects.get(idx="comp-noextra")
        assert source.sku_prefix != "SHOULD-NOT-LAND"


class TestCompetitorsDetail:
    def test_procurement_source_returns_404_through_competitor_facade(self, admin_client, source_factory):
        source_factory(idx="cross-4", kind="procurement")
        resp = admin_client.get("/api/atlas/v2/admin/competitors/cross-4/")
        assert resp.status_code == 404

    def test_monitoring_source_retrieves_ok(self, admin_client, source_factory):
        source_factory(idx="comp-ok", kind="monitoring")
        resp = admin_client.get("/api/atlas/v2/admin/competitors/comp-ok/")
        assert resp.status_code == 200
        assert resp.data["idx"] == "comp-ok"

    def test_response_excludes_procurement_only_fields(self, admin_client, source_factory):
        source_factory(idx="comp-shape", kind="monitoring")
        resp = admin_client.get("/api/atlas/v2/admin/competitors/comp-shape/")
        for field in (
            "sku_prefix",
            "default_feature_set_idx",
            "target_warehouse_code",
            "qty_subtract",
            "qty_minimum",
            "lead_time_days",
            "allow_physical_writes_from_non_primary",
        ):
            assert field not in resp.data


class TestCompetitorsPatchDelete:
    def test_patch_procurement_source_returns_404(self, admin_client, source_factory):
        source_factory(idx="cross-5", kind="procurement")
        resp = admin_client.patch("/api/atlas/v2/admin/competitors/cross-5/", {"name": "X"}, format="json")
        assert resp.status_code == 404

    def test_delete_procurement_source_returns_404(self, admin_client, source_factory):
        source_factory(idx="cross-6", kind="procurement")
        resp = admin_client.delete("/api/atlas/v2/admin/competitors/cross-6/")
        assert resp.status_code == 404
