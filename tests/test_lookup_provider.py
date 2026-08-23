# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the django-lookup provider (plan 03).

The provider is atlas's read boundary for the lookup module — exercised directly, with no
django_lookup dependency: the module mirrors the contract dataclasses on purpose.
"""

from decimal import Decimal

import pytest
from django_pim.models.real_product import RealProduct

from django_atlas.enums import MappingValueModifier, ProductStatus
from django_atlas.models import AttributeMappingTargetType
from django_atlas.services import lookup_provider
from tests.factories import AttributeMappingFactory, MappingProfileFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def source():
    return SourceFactory(idx="acme")


def _mapping(source, source_field: str, target: str, modifier=MappingValueModifier.NONE.value, **kwargs):
    profile = MappingProfileFactory(source=source, idx="default", **kwargs)
    return AttributeMappingFactory(
        profile=profile,
        source_field=source_field,
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=target,
        modifier=modifier,
    )


def test_item_carries_identifiers_name_and_image(source):
    source_product = SourceProductFactory(
        source=source,
        external_id="EXT-1",
        name="Bosch GSR 12V-35",
        ean="5901234123457",
        image_urls=["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        data={"brand": "Bosch", "mpn": "GSR 12V-35", "weight": "1.2"},
    )

    item = lookup_provider.get_item("acme:EXT-1")

    assert item.ref == "acme:EXT-1"
    assert item.gtin == "5901234123457"
    assert item.name_by_lang == {"en": "Bosch GSR 12V-35"}  # the source's default language
    assert (item.brand, item.mpn) == ("Bosch", "GSR 12V-35")
    assert item.attrs == {"weight": "1.2", "width": None, "height": None, "deep": None}
    assert item.image_path_or_url == "https://cdn.example/a.jpg"  # remote — never fetched here
    assert item.updated_at == source_product.modified_at


def test_mapped_source_fields_win_over_the_conventional_keys(source):
    _mapping(source, "producent", "brand")
    _mapping(source, "waga_g", "weight", modifier=MappingValueModifier.GRAMS_TO_KG.value)
    SourceProductFactory(
        source=source, external_id="EXT-2", data={"producent": "Makita", "brand": "Bosch", "waga_g": "1200"}
    )

    item = lookup_provider.get_item("acme:EXT-2")

    assert item.brand == "Makita"
    assert item.attrs["weight"] == Decimal("1.2")  # the mapping's modifier is applied


def test_inactive_profiles_are_ignored(source):
    _mapping(source, "producent", "brand", is_active=False)
    SourceProductFactory(source=source, external_id="EXT-3", data={"producent": "Makita", "manufacturer": "Bosch"})

    assert lookup_provider.get_item("acme:EXT-3").brand == "Bosch"  # fallback key


def test_skip_mappings_are_ignored(source):
    mapping = _mapping(source, "producent", "brand")
    mapping.target_type = AttributeMappingTargetType.SKIP.value
    mapping.save(update_fields=["target_type"])
    SourceProductFactory(source=source, external_id="EXT-9", data={"producent": "Makita", "manufacturer": "Bosch"})

    assert lookup_provider.get_item("acme:EXT-9").brand == "Bosch"


def test_fallback_keys_cover_the_common_spellings(source):
    SourceProductFactory(source=source, external_id="EXT-4", data={"manufacturer": "Bosch", "depth": 12})

    item = lookup_provider.get_item("acme:EXT-4")

    assert (item.brand, item.attrs["deep"]) == ("Bosch", 12)


def test_non_text_brand_becomes_text(source):
    SourceProductFactory(source=source, external_id="EXT-5", data={"brand": 3000})

    assert lookup_provider.get_item("acme:EXT-5").brand == "3000"


def test_iter_items_serves_only_the_unlinked_candidate_pool(source):
    SourceProductFactory(source=source, external_id="FREE")
    SourceProductFactory(source=source, external_id="REJECTED", status=ProductStatus.REJECTED.value)
    SourceProductFactory(
        source=source, external_id="LINKED", real_product=RealProduct.objects.create(sku="SKU-1", ean="")
    )

    assert [item.ref for item in lookup_provider.iter_items()] == ["acme:FREE"]


def test_iter_items_honours_since(source):
    older = SourceProductFactory(source=source, external_id="OLD")
    newer = SourceProductFactory(source=source, external_id="NEW")

    assert [item.ref for item in lookup_provider.iter_items(since=newer.modified_at)] == ["acme:NEW"]
    assert len(list(lookup_provider.iter_items(since=older.modified_at))) == 2


def test_linking_removes_the_item_from_the_pool(source):
    source_product = SourceProductFactory(source=source, external_id="EXT-6")
    assert lookup_provider.get_item("acme:EXT-6").ref == "acme:EXT-6"

    source_product.real_product = RealProduct.objects.create(sku="SKU-2", ean="")
    source_product.save(update_fields=["real_product"])

    with pytest.raises(LookupError):
        lookup_provider.get_item("acme:EXT-6")


@pytest.mark.parametrize("ref", ["acme:NOPE", "no-separator", "unknown-source:EXT-1"])
def test_unknown_ref_raises_lookup_error(source, ref):  # noqa: ARG001 — the source must exist
    with pytest.raises(LookupError):
        lookup_provider.get_item(ref)


def test_basic_and_detail_url_address_the_admin_api(source):
    source_product = SourceProductFactory(
        source=source, external_id="EXT-7", name="Bosch drill", ean="5901234123457", image_urls=["https://cdn/a.jpg"]
    )

    basic = lookup_provider.basic("acme:EXT-7")

    assert (basic.ref, basic.name, basic.gtin) == ("acme:EXT-7", "Bosch drill", "5901234123457")
    assert basic.image_url == "https://cdn/a.jpg"
    assert lookup_provider.detail_url("acme:EXT-7") == f"/api/atlas/v2/admin/products/{source_product.pk}/"


def test_signal_spec_resolves_the_ref(source):
    source_product = SourceProductFactory(source=source, external_id="EXT-8")
    (spec,) = lookup_provider.signal_specs()

    assert (spec["model"], spec["signal"]) == ("django_atlas.SourceProduct", "post_save")
    assert spec["ref"](source_product) == "acme:EXT-8"
