# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Integration tests for GET /sources/{idx}/data-keys/."""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from .factories import SourceFactory, SourceProductFactory

URL = "/api/atlas/v2/admin/sources/{idx}/data-keys/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_data_keys_requires_auth(api_client: APIClient, language, currency):
    source = SourceFactory(idx="auth-sup", default_language=language, default_currency=currency)
    response = api_client.get(URL.format(idx=source.idx))
    assert response.status_code == 401


@pytest.mark.django_db
def test_data_keys_returns_tokens_and_keys(admin_client: APIClient, language, currency):
    source = SourceFactory(idx="happy-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="sku-1", data={"weight_g": "350", "category_path": "Gry"})
    SourceProductFactory(source=source, external_id="sku-2", data={"weight_g": "420", "category_path": "Gry"})

    response = admin_client.get(URL.format(idx=source.idx))

    assert response.status_code == 200
    body = response.json()
    assert body["sample_size"] == 2
    assert [t["key"] for t in body["tokens"]] == ["__name__", "__cost__", "__ean__"]
    keys = {k["key"]: k for k in body["data_keys"]}
    assert keys["weight_g"]["presence_pct"] == 100
    assert keys["category_path"]["presence_pct"] == 100
    assert keys["weight_g"]["type"] == "string"


@pytest.mark.django_db
def test_data_keys_unknown_source_returns_404(admin_client: APIClient):
    response = admin_client.get(URL.format(idx="ghost-source"))
    assert response.status_code == 404


@pytest.mark.django_db
def test_data_keys_invalid_sample_size_returns_400(admin_client: APIClient, language, currency):
    source = SourceFactory(idx="bad-size-sup", default_language=language, default_currency=currency)
    response = admin_client.get(URL.format(idx=source.idx) + "?sample_size=abc")
    assert response.status_code == 400
