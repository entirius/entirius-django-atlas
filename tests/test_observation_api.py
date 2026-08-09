# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the read-only Observation admin API.

`observations/` has no write path anywhere — Observation is append-only end to end.
"""

import pytest
from rest_framework.test import APIClient

from django_atlas.services import observation_service
from tests.factories import SourceFactory

pytestmark = pytest.mark.django_db

URL = "/api/atlas/v2/admin/observations/"


@pytest.fixture
def regular_client(api_client: APIClient, regular_user) -> APIClient:
    api_client.force_authenticate(user=regular_user)
    return api_client


def test_list_unauthorized(api_client):
    assert api_client.get(URL).status_code == 401


def test_list_forbidden_for_regular_user(regular_client):
    assert regular_client.get(URL).status_code == 403


def test_list_returns_paginated_envelope(admin_client):
    source = SourceFactory(kind="monitoring")
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    resp = admin_client.get(URL)
    assert resp.status_code == 200
    assert "count" in resp.data
    assert "results" in resp.data
    assert resp.data["count"] == 1
    entry = resp.data["results"][0]
    assert entry["source_idx"] == source.idx
    assert entry["sku"] == "SKU-1"
    assert entry["kind"] == "monitoring"
    assert entry["value"] == {"price": "1", "currency": "EUR"}


def test_list_filters_by_sku(admin_client):
    source = SourceFactory(kind="monitoring")
    observation_service.record_observation(source=source, sku="SKU-A", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source, sku="SKU-B", value={"price": "2", "currency": "EUR"})
    resp = admin_client.get(URL, {"sku": "SKU-A"})
    assert resp.status_code == 200
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["sku"] == "SKU-A"


def test_list_filters_by_kind(admin_client):
    monitoring = SourceFactory(kind="monitoring")
    enrichment = SourceFactory(kind="enrichment")
    observation_service.record_observation(source=monitoring, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=enrichment, sku="SKU-1", value={"signals": {"tag": "x"}})
    resp = admin_client.get(URL, {"kind": "enrichment"})
    assert resp.status_code == 200
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["kind"] == "enrichment"


def test_list_filters_by_source(admin_client):
    source_a = SourceFactory(idx="obs-src-a", kind="monitoring")
    source_b = SourceFactory(idx="obs-src-b", kind="monitoring")
    observation_service.record_observation(source=source_a, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source_b, sku="SKU-1", value={"price": "2", "currency": "EUR"})
    resp = admin_client.get(URL, {"source": "obs-src-a"})
    assert resp.status_code == 200
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["source_idx"] == "obs-src-a"


def test_list_default_returns_full_timeline_not_collapsed(admin_client):
    source = SourceFactory(kind="monitoring")
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "2", "currency": "EUR"})
    resp = admin_client.get(URL, {"sku": "SKU-1"})
    assert resp.data["count"] == 2


def test_list_latest_per_source_true_collapses(admin_client):
    source = SourceFactory(kind="monitoring")
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "1", "currency": "EUR"})
    observation_service.record_observation(source=source, sku="SKU-1", value={"price": "2", "currency": "EUR"})
    resp = admin_client.get(URL, {"sku": "SKU-1", "latest_per_source": "true"})
    assert resp.data["count"] == 1
    assert resp.data["results"][0]["value"] == {"price": "2", "currency": "EUR"}
