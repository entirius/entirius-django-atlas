# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stage 6: Admin API v2 — custom actions coverage.

Tests endpoints that are NOT plain CRUD: feed trigger/test, mapping validate,
review actions (approve/reject/skip/queue + bulk variants), push/force-repush,
bulk push, event acknowledge, product-link set-primary, plus v2 error format check.

Push services are mocked at the view import path — this tests the API contract,
not the full PIM integration (covered in test_push_service.py).
"""

import pytest
from rest_framework.test import APIClient

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def source(db, language, currency):
    from django_atlas.models import Source

    return Source.objects.create(
        idx="source-actions", name="Actions", default_language=language, default_currency=currency
    )


@pytest.fixture
def feed(db, source):
    from django_atlas.models import SourceFeed

    return SourceFeed.objects.create(
        source=source,
        idx="feed-actions",
        connector_kind="xml_feed",
        feed_config={
            "feed_url": "https://x/y.xml",
            "field_mapping": {"external_id": ".//id", "name": ".//name", "cost": ".//cost"},
        },
    )


@pytest.fixture
def source_product(db, source):
    from django_atlas.models import SourceProduct

    return SourceProduct.objects.create(source=source, external_id="EXT-001", name="Item One", status="queued")


@pytest.fixture
def regular_client(api_client: APIClient, regular_user) -> APIClient:
    api_client.force_authenticate(user=regular_user)
    return api_client


# -----------------------------------------------------------------------------
# Feed trigger / test_feed (5 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestFeedTrigger:
    def url(self, source, feed):
        return f"/api/atlas/v2/admin/sources/{source.idx}/feeds/{feed.idx}/trigger/"

    def test_unauthorized(self, api_client, source, feed):
        assert api_client.post(self.url(source, feed), {}, format="json").status_code == 401

    def test_forbidden_for_regular_user(self, regular_client, source, feed):
        assert regular_client.post(self.url(source, feed), {}, format="json").status_code == 403

    def test_404_unknown_feed(self, admin_client, source):
        url = f"/api/atlas/v2/admin/sources/{source.idx}/feeds/missing/trigger/"
        assert admin_client.post(url, {}, format="json").status_code == 404

    def test_happy(self, monkeypatch, admin_client, source, feed):
        from django_atlas.api.admin.views import feed_views as views_module

        class _FakeLog:
            run_id = "abc-123"
            status = "success"

        monkeypatch.setattr(views_module.feed_service, "trigger_feed_run", lambda *a, **kw: _FakeLog())
        resp = admin_client.post(self.url(source, feed), {"mode": "full"}, format="json")
        assert resp.status_code == 200
        assert resp.data["run_id"] == "abc-123"


@pytest.mark.django_db
class TestFeedTestSample:
    def url(self, source, feed):
        return f"/api/atlas/v2/admin/sources/{source.idx}/feeds/{feed.idx}/test/"

    def test_sync_returns_list(self, monkeypatch, admin_client, source, feed):
        from django_atlas.api.admin.views import feed_views as views_module

        monkeypatch.setattr(
            views_module.feed_service,
            "test_feed",
            lambda *a, **kw: [{"external_id": "1", "name": "X", "cost": "10.00", "currency": "EUR"}],
        )
        resp = admin_client.post(self.url(source, feed), format="json")
        assert resp.status_code == 200
        assert resp.data["is_async"] is False
        assert resp.data["raw_products"][0]["external_id"] == "1"

    def test_async_returns_dispatch_dict(self, monkeypatch, admin_client, source, feed):
        from django_atlas.api.admin.views import feed_views as views_module

        monkeypatch.setattr(
            views_module.feed_service,
            "test_feed",
            lambda *a, **kw: {"status": "dispatched", "task_id": "t-1", "run_id": "r-1"},
        )
        resp = admin_client.post(self.url(source, feed), format="json")
        assert resp.status_code == 200
        assert resp.data["is_async"] is True
        assert resp.data["status"] == "dispatched"

    def test_async_suppressed(self, monkeypatch, admin_client, source, feed):
        from django_atlas.api.admin.views import feed_views as views_module

        monkeypatch.setattr(
            views_module.feed_service,
            "test_feed",
            lambda *a, **kw: {"status": "suppressed", "reason": "scraper_dispatch_disabled"},
        )
        resp = admin_client.post(self.url(source, feed), format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "suppressed"


# -----------------------------------------------------------------------------
# Mapping validate (2 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMappingValidate:
    def test_returns_ok_true(self, admin_client, source, pim_channel):
        from django_atlas.services import mapping_service

        profile = mapping_service.create_profile(source.idx, idx="p1", name="P1", target_channel_idxs=[pim_channel.idx])
        url = f"/api/atlas/v2/admin/sources/{source.idx}/mapping-profiles/{profile.idx}/validate/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["ok"] is True

    def test_returns_errors_when_feature_set_missing(self, admin_client, source, pim_channel):
        from django_atlas.services import mapping_service

        profile = mapping_service.create_profile(
            source.idx, idx="p1", name="P1", target_channel_idxs=[pim_channel.idx], feature_set_idx="missing-fs"
        )
        url = f"/api/atlas/v2/admin/sources/{source.idx}/mapping-profiles/{profile.idx}/validate/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["ok"] is False
        assert any("feature_set" in e.lower() for e in resp.data["errors"])


# -----------------------------------------------------------------------------
# Product review actions (4 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProductReviewActions:
    def test_approve_happy(self, admin_client, source_product):
        url = f"/api/atlas/v2/admin/products/{source_product.id}/approve/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "approved"

    def test_approve_invalid_transition(self, admin_client, source_product):
        source_product.status = "pushed"
        source_product.save()
        # pushed -> approved IS allowed (force re-push intent), so try a bad path:
        # rejected -> pushed_pending_images is not allowed, but we can't use that endpoint;
        # instead test with new -> push (which approve handles via transition).
        # Use the actual invalid path: pushed_pending_images -> approved not allowed.
        source_product.status = "pushed_pending_images"
        source_product.save()
        url = f"/api/atlas/v2/admin/products/{source_product.id}/approve/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 400

    def test_reject(self, admin_client, source_product):
        url = f"/api/atlas/v2/admin/products/{source_product.id}/reject/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "rejected"

    def test_skip(self, admin_client, source_product):
        url = f"/api/atlas/v2/admin/products/{source_product.id}/skip/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "new"

    def test_queue(self, admin_client, source_product):
        source_product.status = "rejected"
        source_product.save()
        url = f"/api/atlas/v2/admin/products/{source_product.id}/queue/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "queued"

    @pytest.mark.parametrize("action", ["approve", "reject", "skip", "queue"])
    def test_action_unknown_pk_returns_404(self, admin_client, action):
        url = f"/api/atlas/v2/admin/products/999999/{action}/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 404

    def test_approve_monitoring_source_product_returns_v2_400(self, admin_client, language, currency):
        """Review kind-aware gate: push-adjacent transitions (approve/queue)
        are blocked for monitoring sources — only reject/skip stay available."""
        from django_atlas.models import Source, SourceProduct

        monitoring_source = Source.objects.create(
            idx="monitoring-review",
            name="Monitoring",
            kind="monitoring",
            default_language=language,
            default_currency=currency,
        )
        sp = SourceProduct.objects.create(
            source=monitoring_source, external_id="MON-REVIEW-1", name="Monitored Item", status="queued"
        )
        url = f"/api/atlas/v2/admin/products/{sp.id}/approve/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 400
        assert resp.data["error"] == "VALIDATION_ERROR"

    def test_queue_monitoring_source_product_returns_v2_400(self, admin_client, language, currency):
        from django_atlas.models import Source, SourceProduct

        monitoring_source = Source.objects.create(
            idx="monitoring-review-2",
            name="Monitoring",
            kind="monitoring",
            default_language=language,
            default_currency=currency,
        )
        sp = SourceProduct.objects.create(
            source=monitoring_source, external_id="MON-REVIEW-2", name="Monitored Item", status="rejected"
        )
        url = f"/api/atlas/v2/admin/products/{sp.id}/queue/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 400

    def test_reject_monitoring_source_product_still_allowed(self, admin_client, language, currency):
        """reject/skip stay available for monitoring — only push-adjacent transitions gate."""
        from django_atlas.models import Source, SourceProduct

        monitoring_source = Source.objects.create(
            idx="monitoring-review-3",
            name="Monitoring",
            kind="monitoring",
            default_language=language,
            default_currency=currency,
        )
        sp = SourceProduct.objects.create(
            source=monitoring_source, external_id="MON-REVIEW-3", name="Monitored Item", status="queued"
        )
        url = f"/api/atlas/v2/admin/products/{sp.id}/reject/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["status"] == "rejected"


# -----------------------------------------------------------------------------
# Product bulk actions (2 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProductBulkActions:
    def test_bulk_approve(self, admin_client, source):
        from django_atlas.models import SourceProduct

        sp_ids = []
        for i in range(3):
            sp = SourceProduct.objects.create(source=source, external_id=f"EXT-{i}", name=f"P{i}", status="queued")
            sp_ids.append(sp.id)
        resp = admin_client.post("/api/atlas/v2/admin/products/bulk-approve/", {"ids": sp_ids}, format="json")
        assert resp.status_code == 200
        assert resp.data["success"] == 3

    def test_bulk_reject(self, admin_client, source):
        from django_atlas.models import SourceProduct

        sp = SourceProduct.objects.create(source=source, external_id="EXT-1", name="P1", status="queued")
        resp = admin_client.post("/api/atlas/v2/admin/products/bulk-reject/", {"ids": [sp.id]}, format="json")
        assert resp.status_code == 200
        assert resp.data["success"] == 1


# -----------------------------------------------------------------------------
# Product push (3 tests, mocked)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProductPush:
    def test_push_happy(self, monkeypatch, admin_client, source_product):
        from django_atlas.api.admin.views import product_views as views_module

        source_product.status = "approved"
        source_product.save()
        monkeypatch.setattr(
            views_module.push_service, "push_source_product", lambda pk, user, *, event_sink=None: ["product-1"]
        )
        url = f"/api/atlas/v2/admin/products/{source_product.id}/push/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 200
        assert resp.data["pushed_channels_count"] == 1

    def test_push_invalid_status(self, monkeypatch, admin_client, source_product):
        from django_atlas.api.admin.views import product_views as views_module

        def raise_value(*a, **kw):
            raise ValueError("invalid status")

        monkeypatch.setattr(views_module.push_service, "push_source_product", raise_value)
        url = f"/api/atlas/v2/admin/products/{source_product.id}/push/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 400

    def test_force_repush_invalid_status(self, monkeypatch, admin_client, source_product):
        from django_atlas.api.admin.views import product_views as views_module

        def raise_value(*a, **kw):
            raise ValueError("expected pushed")

        monkeypatch.setattr(views_module.push_service, "force_repush_source_product", raise_value)
        url = f"/api/atlas/v2/admin/products/{source_product.id}/force-repush/"
        resp = admin_client.post(url, format="json")
        assert resp.status_code == 400

    def test_push_unknown_pk_returns_404(self, admin_client):
        resp = admin_client.post("/api/atlas/v2/admin/products/999999/push/", format="json")
        assert resp.status_code == 404

    def test_force_repush_unknown_pk_returns_404(self, admin_client):
        resp = admin_client.post("/api/atlas/v2/admin/products/999999/force-repush/", format="json")
        assert resp.status_code == 404


# -----------------------------------------------------------------------------
# Bulk push (1 test, mocked)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestBulkPush:
    URL = "/api/atlas/v2/admin/push/"

    def test_bulk_push_source_idx(self, monkeypatch, admin_client, source):
        from django_atlas.api.admin.views import push_views as views_module

        monkeypatch.setattr(
            views_module.push_service,
            "push_approved_for_source",
            lambda *a, **kw: {"success": 5, "failed": 0, "preflight_failed": False, "errors": []},
        )
        resp = admin_client.post(self.URL, {"source_idx": source.idx}, format="json")
        assert resp.status_code == 200
        assert resp.data["success"] == 5
        assert resp.data["sources_processed"] == 1


# -----------------------------------------------------------------------------
# Event acknowledge (2 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestEventAcknowledge:
    def test_ack_happy(self, admin_client, source):
        from django_atlas.services import event_service

        ev = event_service.record(event_type="push_succeeded", severity="info", source=source, message="ok")
        resp = admin_client.post(f"/api/atlas/v2/admin/events/{ev.id}/acknowledge/", format="json")
        assert resp.status_code == 200
        assert resp.data["acknowledged_at"] is not None

    def test_ack_idempotent(self, admin_client, source):
        from django_atlas.services import event_service

        ev = event_service.record(event_type="push_succeeded", severity="info", source=source, message="ok")
        admin_client.post(f"/api/atlas/v2/admin/events/{ev.id}/acknowledge/", format="json")
        # Re-ack should still 200 (idempotent: returns existing without changes).
        resp = admin_client.post(f"/api/atlas/v2/admin/events/{ev.id}/acknowledge/", format="json")
        assert resp.status_code == 200


# -----------------------------------------------------------------------------
# ProductLink set-primary (2 tests)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProductLinkSetPrimary:
    def test_set_primary_unsets_siblings(self, admin_client, source, language, currency):
        from django_atlas.models import Source, SourceProductLink

        other = Source.objects.create(
            idx="sup-other", name="Other", default_language=language, default_currency=currency
        )
        link_a = SourceProductLink.objects.create(real_product_sku="SKU-1", source=source, is_primary=True)
        link_b = SourceProductLink.objects.create(real_product_sku="SKU-1", source=other, is_primary=False)
        resp = admin_client.post(f"/api/atlas/v2/admin/product-links/{link_b.id}/set-primary/", format="json")
        assert resp.status_code == 200
        link_a.refresh_from_db()
        link_b.refresh_from_db()
        assert link_a.is_primary is False
        assert link_b.is_primary is True

    def test_set_primary_404(self, admin_client):
        resp = admin_client.post("/api/atlas/v2/admin/product-links/9999/set-primary/", format="json")
        assert resp.status_code == 404


# -----------------------------------------------------------------------------
# V2 validation error structure (1 test)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestV2ErrorStructure:
    def test_validation_error_returns_v2_envelope(self, admin_client):
        # Trigger a Pydantic validation error (missing required fields)
        resp = admin_client.post("/api/atlas/v2/admin/sources/", {"idx": "x", "name": ""}, format="json")
        assert resp.status_code == 400
        # v2 envelope keys per django_utils.api.v2_errors.ErrorResponse
        assert "error" in resp.data
        assert "message" in resp.data
        assert "debug_id" in resp.data
        assert "details" in resp.data
        assert resp.data["error"] == "VALIDATION_ERROR"
