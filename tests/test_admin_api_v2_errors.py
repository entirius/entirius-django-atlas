# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""V2 error shape contract — every admin endpoint returns
`{error, message, debug_id, details}` for non-pydantic errors as well.

Previously these endpoints returned legacy `{detail: "..."}` because views
caught ValueError + returned Response directly, bypassing the v2 handler.
Fix: views raise drf_exceptions.* → handler emits structured shape.
"""

import pytest

from .factories import MappingProfileFactory, SourceFactory

V2_KEYS = {"error", "message", "debug_id", "details"}


def _assert_v2_envelope(payload: dict) -> None:
    assert V2_KEYS.issubset(payload.keys()), f"missing v2 keys; got {set(payload.keys())}"
    assert isinstance(payload["debug_id"], str) and len(payload["debug_id"]) == 8
    assert isinstance(payload["details"], list)


@pytest.mark.django_db
class TestV2EnvelopeNotFound:
    """v2_exception_handler emits canonical status messages for non-validation errors.

    Concrete exception text (e.g. ``"Source 'X' not found"``) lands in the warning
    log entry with the same ``debug_id`` — frontend correlates via debug_id for support.
    Upstream django-utils enhancement candidate: pass detail through to ``details[]``.
    """

    def test_source_retrieve_unknown_idx_returns_v2_404(self, admin_client):
        resp = admin_client.get("/api/atlas/v2/admin/sources/no-such-source/")
        assert resp.status_code == 404
        _assert_v2_envelope(resp.data)
        assert resp.data["error"] == "NOT_FOUND"
        assert resp.data["message"]  # non-empty

    def test_feed_list_unknown_source_returns_v2_404(self, admin_client):
        resp = admin_client.get("/api/atlas/v2/admin/sources/ghost-source/feeds/")
        assert resp.status_code == 404
        _assert_v2_envelope(resp.data)
        assert resp.data["error"] == "NOT_FOUND"

    def test_mapping_profile_list_unknown_source_returns_v2_404(self, admin_client):
        resp = admin_client.get("/api/atlas/v2/admin/sources/missing/mapping-profiles/")
        assert resp.status_code == 404
        _assert_v2_envelope(resp.data)
        assert resp.data["error"] == "NOT_FOUND"


@pytest.mark.django_db
class TestV2EnvelopeValidation:
    def test_attribute_mapping_bogus_target_returns_v2_400(
        self, admin_client, language, currency, country, pim_channel
    ):
        source = SourceFactory(idx="vendor-x", default_language=language, default_currency=currency, country=country)
        profile = MappingProfileFactory(source=source, idx="prof-v2", target_channel_idxs=[pim_channel.idx])

        resp = admin_client.post(
            f"/api/atlas/v2/admin/mapping-profiles/{profile.pk}/attribute-mappings/",
            {
                "source_field": "__name__",
                "target_type": "feature",
                "target_identifier": "nonexistent_feature_xyz",
                "is_required": False,
            },
            format="json",
        )
        # ValueError("PIM feature 'X' not found") from service → NotFound via _helpers.
        assert resp.status_code in (400, 404)
        _assert_v2_envelope(resp.data)
        assert resp.data["error"] in {"VALIDATION_ERROR", "NOT_FOUND"}
        # 400 path keeps detail in `details[]`; 404 path keeps it only in logs (v2 handler
        # quirk — generic status message). Accept either, document the asymmetry.
        body_text = resp.data["message"] + " ".join(str(d.get("description", "")) for d in resp.data["details"])
        if resp.data["error"] == "VALIDATION_ERROR":
            assert "nonexistent_feature_xyz" in body_text

    def test_data_keys_bad_sample_size_returns_v2_400(self, admin_client, language, currency, country):
        SourceFactory(idx="sup-keys", default_language=language, default_currency=currency, country=country)
        resp = admin_client.get("/api/atlas/v2/admin/sources/sup-keys/data-keys/?sample_size=abc")
        assert resp.status_code == 400
        _assert_v2_envelope(resp.data)
        assert resp.data["error"] == "VALIDATION_ERROR"
