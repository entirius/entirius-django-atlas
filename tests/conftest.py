# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest
from django.contrib.auth.models import User
from django_regional.models.country import Country
from django_regional.models.currency import Currency
from django_regional.models.language import Language
from rest_framework.test import APIClient


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def admin_user(db) -> User:
    return User.objects.create_superuser(username="admin", email="admin@test.local", password="admin-pass")


@pytest.fixture
def regular_user(db) -> User:
    return User.objects.create_user(username="user", email="user@test.local", password="user-pass")


@pytest.fixture
def admin_client(admin_user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=admin_user)
    return client


@pytest.fixture
def language(db) -> Language:
    obj, _ = Language.objects.get_or_create(
        iso2="en", defaults={"iso3": "eng", "name_en": "English", "name_pl": "angielski"}
    )
    return obj


@pytest.fixture
def currency(db) -> Currency:
    obj, _ = Currency.objects.get_or_create(
        iso3="EUR", defaults={"name_en": "Euro", "name_pl": "Euro", "symbol": "EUR"}
    )
    return obj


@pytest.fixture
def country(db) -> Country:
    existing = Country.objects.filter(iso2="PL").first()
    if existing is not None:
        return existing
    Country.objects.bulk_create([Country(iso2="PL", iso3="POL", name_en="Poland", name_pl="Polska")])
    return Country.objects.get(iso2="PL")


@pytest.fixture
def pim_channel(db, language, currency):
    """Minimal PIM Channel fixture for mapping validation tests."""
    from django_pim.models.channel import Channel

    obj, _ = Channel.objects.get_or_create(
        idx="ch-test", defaults={"name": "Test Channel", "default_language": language, "default_currency": currency}
    )
    return obj


@pytest.fixture
def pim_channel_factory(db, language, currency):
    """Factory callable returning a PIM Channel by idx."""
    from django_pim.models.channel import Channel

    def _make(idx: str, name: str | None = None):
        obj, _ = Channel.objects.get_or_create(
            idx=idx,
            defaults={"name": name or f"Channel {idx}", "default_language": language, "default_currency": currency},
        )
        return obj

    return _make


@pytest.fixture
def pim_feature(db):
    """Minimal PIM Feature fixture (BUSINESS_UNIT scope, BOOL type)."""
    from django_pim.models.feature import Feature, FeatureScopeEnum, FeatureTypeEnum

    obj, _ = Feature.objects.get_or_create(
        idx="test-feature",
        defaults={
            "scope": FeatureScopeEnum.BUSINESS_UNIT,
            "feature_type": FeatureTypeEnum.BOOL,
            "name_t9n": {"en": "Test Feature"},
            "display_order": 100,
        },
    )
    return obj


@pytest.fixture
def pim_feature_factory(db):
    """Factory callable returning a PIM Feature by idx."""
    from django_pim.models.feature import Feature, FeatureScopeEnum, FeatureTypeEnum

    def _make(idx: str, name: str | None = None):
        obj, _ = Feature.objects.get_or_create(
            idx=idx,
            defaults={
                "scope": FeatureScopeEnum.BUSINESS_UNIT,
                "feature_type": FeatureTypeEnum.BOOL,
                "name_t9n": {"en": name or idx},
                "display_order": 100,
            },
        )
        return obj

    return _make


@pytest.fixture
def pim_category_factory(db):
    """Factory callable creating a PIM ProductCategory in a given channel."""
    from django_pim.models.product_category import ProductCategory

    def _make(channel, idx: str):
        obj, _ = ProductCategory.objects.get_or_create(
            shop=channel, idx=idx, defaults={"tree_deep": 0, "is_active": True}
        )
        return obj

    return _make


@pytest.fixture
def pim_feature_set_factory(db):
    """Factory callable returning a PIM FeatureSet by idx."""
    from django_pim.models.feature_set import FeatureSet

    def _make(idx: str = "fs-test"):
        obj, _ = FeatureSet.objects.get_or_create(idx=idx, defaults={"name": idx})
        return obj

    return _make


@pytest.fixture
def pim_feature_set(pim_feature_set_factory):
    return pim_feature_set_factory("fs-test")


@pytest.fixture
def pim_typed_feature_factory(db):
    """Factory creating a PIM Feature of a given FeatureType (via feature_type int)."""
    from django_pim.models.feature import Feature, FeatureScopeEnum

    def _make(idx: str, feature_type: int):
        obj, _ = Feature.objects.get_or_create(
            idx=idx,
            defaults={
                "scope": FeatureScopeEnum.BUSINESS_UNIT,
                "feature_type": feature_type,
                "name_t9n": {"en": idx},
                "display_order": 100,
            },
        )
        if obj.feature_type != feature_type:
            obj.feature_type = feature_type
            obj.save(update_fields=["feature_type"])
        return obj

    return _make


@pytest.fixture
def pim_attribute_factory(db):
    """Factory creating a PIM Attribute under a Feature with given idx."""
    from django_pim.models.attribute import Attribute

    def _make(feature, idx: str):
        obj, _ = Attribute.objects.get_or_create(feature=feature, idx=idx, defaults={"display_order": 100})
        return obj

    return _make
