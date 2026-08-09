# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for data_inspector_service."""

from decimal import Decimal

import pytest
from django.core.cache import cache

from django_atlas.services import data_inspector_service

from .factories import SourceFactory, SourceProductFactory


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_list_keys_empty_source_returns_tokens_only(language, currency):
    source = SourceFactory(idx="empty-sup", default_language=language, default_currency=currency)

    payload = data_inspector_service.list_keys(source.idx)

    assert payload["sample_size"] == 0
    assert payload["data_keys"] == []
    assert [t["key"] for t in payload["tokens"]] == ["__name__", "__cost__", "__ean__"]


@pytest.mark.django_db
def test_list_keys_flattens_nested_dict_dot_paths(language, currency):
    source = SourceFactory(idx="nest-sup", default_language=language, default_currency=currency)
    SourceProductFactory(
        source=source,
        external_id="sku-1",
        cost=Decimal("10.00"),
        data={"info": {"options": {"value": "A"}}, "weight_g": "350"},
    )

    payload = data_inspector_service.list_keys(source.idx)

    keys = {item["key"] for item in payload["data_keys"]}
    assert "info.options.value" in keys
    assert "weight_g" in keys
    assert payload["sample_size"] == 1


@pytest.mark.django_db
def test_list_keys_computes_presence_pct_and_sorts(language, currency):
    source = SourceFactory(idx="pres-sup", default_language=language, default_currency=currency)
    # 4 SP rows: 'common' in all 4 (100%), 'rare' in 1 (25%)
    SourceProductFactory(source=source, external_id="a", data={"common": "x", "rare": "z"})
    SourceProductFactory(source=source, external_id="b", data={"common": "y"})
    SourceProductFactory(source=source, external_id="c", data={"common": "y"})
    SourceProductFactory(source=source, external_id="d", data={"common": "y"})

    payload = data_inspector_service.list_keys(source.idx)

    keys = {item["key"]: item for item in payload["data_keys"]}
    assert keys["common"]["presence_pct"] == 100
    assert keys["rare"]["presence_pct"] == 25
    # Sort: presence DESC, key ASC
    assert payload["data_keys"][0]["key"] == "common"
    assert payload["data_keys"][1]["key"] == "rare"


@pytest.mark.django_db
def test_arrays_treated_as_scalar_leaf(language, currency):
    source = SourceFactory(idx="arr-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="sku-1", data={"image_urls": ["a", "b", "c"], "tags": ["sale"]})

    payload = data_inspector_service.list_keys(source.idx)
    keys = {item["key"]: item for item in payload["data_keys"]}
    assert keys["image_urls"]["type"] == "array"
    # No expanded paths like image_urls.0
    assert not any(k.startswith("image_urls.") for k in keys)


@pytest.mark.django_db
def test_cache_hit_avoids_db_then_invalidate_refreshes(language, currency):
    source = SourceFactory(idx="cache-sup", default_language=language, default_currency=currency)
    sp = SourceProductFactory(source=source, external_id="sku-1", data={"foo": 1})

    first = data_inspector_service.list_keys(source.idx)
    assert {k["key"] for k in first["data_keys"]} == {"foo"}

    # Mutate underlying data — cache still serves old payload
    sp.data = {"bar": 2}
    sp.save(update_fields=["data"])
    cached = data_inspector_service.list_keys(source.idx)
    assert {k["key"] for k in cached["data_keys"]} == {"foo"}, "stale cache expected"

    # Invalidate — now sees fresh data
    data_inspector_service.invalidate(source.idx)
    refreshed = data_inspector_service.list_keys(source.idx)
    assert {k["key"] for k in refreshed["data_keys"]} == {"bar"}


@pytest.mark.django_db
def test_list_keys_missing_source_raises_value_error(language):
    with pytest.raises(ValueError, match="not found"):
        data_inspector_service.list_keys("does-not-exist")


@pytest.mark.django_db
def test_list_keys_invalid_sample_size_raises(language):
    with pytest.raises(ValueError, match="sample_size"):
        data_inspector_service.list_keys("any", sample_size=0)


# ---------------------------------------------------------------------------
# list_values
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_values_groups_and_counts_distinct_values(language, currency):
    source = SourceFactory(idx="val-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"category_path": "Pozostałe"})
    SourceProductFactory(source=source, external_id="b", data={"category_path": "Pozostałe"})
    SourceProductFactory(source=source, external_id="c", data={"category_path": "Dom i ogród"})
    SourceProductFactory(source=source, external_id="d", data={"other_key": "x"})  # no category_path

    payload = data_inspector_service.list_values(source.idx, "category_path")

    assert payload["source_field"] == "category_path"
    assert payload["truncated"] is False
    assert payload["total_distinct"] is None  # only set when truncated
    by_value = {v["value"]: v["count"] for v in payload["values"]}
    assert by_value == {"Pozostałe": 2, "Dom i ogród": 1}


@pytest.mark.django_db
def test_list_values_dot_path_traverses_nested_json(language, currency):
    source = SourceFactory(idx="nest-val-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"info": {"options": {"value": "Red"}}})
    SourceProductFactory(source=source, external_id="b", data={"info": {"options": {"value": "Red"}}})
    SourceProductFactory(source=source, external_id="c", data={"info": {"options": {"value": "Blue"}}})

    payload = data_inspector_service.list_values(source.idx, "info.options.value")

    by_value = {v["value"]: v["count"] for v in payload["values"]}
    assert by_value == {"Red": 2, "Blue": 1}


@pytest.mark.django_db
def test_list_values_truncation_sets_total_distinct(language, currency):
    source = SourceFactory(idx="trunc-sup", default_language=language, default_currency=currency)
    for i in range(7):
        SourceProductFactory(source=source, external_id=f"sku-{i}", data={"k": f"val-{i}"})

    payload = data_inspector_service.list_values(source.idx, "k", limit=3)

    assert payload["truncated"] is True
    assert len(payload["values"]) == 3
    assert payload["total_distinct"] == 7


@pytest.mark.django_db
def test_list_values_missing_source_field_returns_empty(language, currency):
    source = SourceFactory(idx="empty-sf-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"other": "x"})

    payload = data_inspector_service.list_values(source.idx, "nonexistent_key")

    assert payload["values"] == []
    assert payload["truncated"] is False
    assert payload["total_distinct"] is None


@pytest.mark.django_db
def test_list_values_cache_invalidate_drops_all_source_field_slots(language, currency):
    source = SourceFactory(idx="inv-sup", default_language=language, default_currency=currency)
    SourceProductFactory(source=source, external_id="a", data={"cat": "x", "color": "red"})

    # Prime caches for 2 different source_fields
    data_inspector_service.list_values(source.idx, "cat")
    data_inspector_service.list_values(source.idx, "color")
    assert cache.get(data_inspector_service.cache_key_values(source.idx, "cat")) is not None
    assert cache.get(data_inspector_service.cache_key_values(source.idx, "color")) is not None

    data_inspector_service.invalidate(source.idx)

    assert cache.get(data_inspector_service.cache_key_values(source.idx, "cat")) is None
    assert cache.get(data_inspector_service.cache_key_values(source.idx, "color")) is None
    assert cache.get(data_inspector_service.cache_key_values_registry(source.idx)) is None


@pytest.mark.django_db
def test_list_values_invalid_args_raise(language, currency):
    source = SourceFactory(idx="args-sup", default_language=language, default_currency=currency)
    with pytest.raises(ValueError, match="source_field"):
        data_inspector_service.list_values(source.idx, "")
    with pytest.raises(ValueError, match="limit"):
        data_inspector_service.list_values(source.idx, "k", limit=0)


@pytest.mark.django_db
def test_list_values_missing_source_raises(language):
    with pytest.raises(ValueError, match="not found"):
        data_inspector_service.list_values("ghost-source", "k")
