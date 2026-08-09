# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest
from django.db import IntegrityError

from django_atlas.enums import EventType
from django_atlas.models import (
    AttributeMappingTargetType,
    IntegrationEvent,
    SourceAttributeMapping,
    SourceMappingProfile,
)
from django_atlas.services import mapping_service
from tests.factories import MappingProfileFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Profile creation / constraints (1-4)
# ---------------------------------------------------------------------------


def test_create_profile_creates_record(pim_channel_factory):
    pim_channel_factory("ch-1")
    source = SourceFactory()

    profile = mapping_service.create_profile(
        source.idx, idx="profile-1", name="Profile 1", target_channel_idxs=["ch-1"]
    )

    assert profile.pk is not None
    assert profile.target_channel_idxs == ["ch-1"]
    assert profile.is_active is True


def test_create_profile_duplicate_idx_raises(pim_channel_factory):
    pim_channel_factory("ch-1")
    source = SourceFactory()
    mapping_service.create_profile(source.idx, idx="profile-1", name="P1", target_channel_idxs=["ch-1"])

    with pytest.raises(IntegrityError):
        # Use raw model create (mapping_service would also work here, both must violate uq).
        SourceMappingProfile.objects.create(source=source, idx="profile-1", name="dup", target_channel_idxs=[])


def test_unique_constraint_source_idx():
    source = SourceFactory()
    SourceMappingProfile.objects.create(source=source, idx="p", name="P", target_channel_idxs=[])
    with pytest.raises(IntegrityError):
        SourceMappingProfile.objects.create(source=source, idx="p", name="dup", target_channel_idxs=[])


def test_default_target_channels_empty_and_active_true():
    source = SourceFactory()
    profile = SourceMappingProfile.objects.create(source=source, idx="p", name="P")

    assert profile.target_channel_idxs == []
    assert profile.is_active is True


# ---------------------------------------------------------------------------
# Attribute mapping (5-11)
# ---------------------------------------------------------------------------


def test_add_attribute_mapping_feature_existing(pim_feature):
    profile = MappingProfileFactory()

    mapping = mapping_service.add_attribute_mapping(
        profile,
        source_field="color",
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier=pim_feature.idx,
    )

    assert mapping.pk is not None
    assert mapping.target_identifier == pim_feature.idx


def test_add_attribute_mapping_feature_missing_raises():
    profile = MappingProfileFactory()

    with pytest.raises(ValueError, match="PIM feature 'nope' not found"):
        mapping_service.add_attribute_mapping(
            profile, source_field="x", target_type=AttributeMappingTargetType.FEATURE.value, target_identifier="nope"
        )


def test_add_attribute_mapping_real_product_invalid_field_raises():
    profile = MappingProfileFactory()

    with pytest.raises(ValueError, match="not a valid RealProduct field"):
        mapping_service.add_attribute_mapping(
            profile,
            source_field="x",
            target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
            target_identifier="color",
        )


def test_add_attribute_mapping_real_product_valid_field():
    profile = MappingProfileFactory()

    mapping = mapping_service.add_attribute_mapping(
        profile,
        source_field="weight_g",
        target_type=AttributeMappingTargetType.REAL_PRODUCT.value,
        target_identifier="weight",
    )

    assert mapping.target_identifier == "weight"


def test_add_attribute_mapping_skip_always_ok():
    profile = MappingProfileFactory()

    mapping = mapping_service.add_attribute_mapping(
        profile, source_field="ignored_field", target_type=AttributeMappingTargetType.SKIP.value, target_identifier=""
    )

    assert mapping.target_type == AttributeMappingTargetType.SKIP.value


def test_unique_constraint_profile_source_field():
    profile = MappingProfileFactory()
    SourceAttributeMapping.objects.create(
        profile=profile, source_field="name", target_type=AttributeMappingTargetType.SKIP.value
    )
    with pytest.raises(IntegrityError):
        SourceAttributeMapping.objects.create(
            profile=profile, source_field="name", target_type=AttributeMappingTargetType.SKIP.value
        )


def test_update_attribute_mapping_revalidates_target(pim_feature_factory):
    pim_feature_factory("feat-old")
    pim_feature_factory("feat-new")

    profile = MappingProfileFactory()
    mapping = mapping_service.add_attribute_mapping(
        profile, source_field="x", target_type=AttributeMappingTargetType.FEATURE.value, target_identifier="feat-old"
    )

    updated = mapping_service.update_attribute_mapping(mapping.pk, target_identifier="feat-new")
    assert updated.target_identifier == "feat-new"

    with pytest.raises(ValueError, match="PIM feature 'missing-feat' not found"):
        mapping_service.update_attribute_mapping(mapping.pk, target_identifier="missing-feat")


# ---------------------------------------------------------------------------
# Category mapping (12-16)
# ---------------------------------------------------------------------------


def test_add_category_mapping_existing_in_channel(pim_channel_factory, pim_category_factory):
    channel = pim_channel_factory("ch-1")
    pim_category_factory(channel, "cat-a")
    profile = MappingProfileFactory(target_channel_idxs=["ch-1"])

    mapping = mapping_service.add_category_mapping(
        profile, source_field="category", source_value="Shoes", target_category_idx="cat-a"
    )

    assert mapping.target_category_idx == "cat-a"


def test_add_category_mapping_missing_in_all_channels_raises(pim_channel_factory):
    pim_channel_factory("ch-1")
    pim_channel_factory("ch-2")
    profile = MappingProfileFactory(target_channel_idxs=["ch-1", "ch-2"])

    with pytest.raises(ValueError, match="PIM category 'cat-missing' not found"):
        mapping_service.add_category_mapping(
            profile, source_field="category", source_value="X", target_category_idx="cat-missing"
        )


def test_add_category_mapping_partial_match_ok(pim_channel_factory, pim_category_factory):
    ch1 = pim_channel_factory("ch-1")
    pim_channel_factory("ch-2")
    pim_category_factory(ch1, "cat-a")
    profile = MappingProfileFactory(target_channel_idxs=["ch-1", "ch-2"])

    mapping = mapping_service.add_category_mapping(
        profile, source_field="category", source_value="X", target_category_idx="cat-a"
    )

    assert mapping.target_category_idx == "cat-a"


def test_add_category_mapping_without_target_channels_raises():
    profile = MappingProfileFactory(target_channel_idxs=[])

    with pytest.raises(ValueError, match="must have at least one target channel"):
        mapping_service.add_category_mapping(
            profile, source_field="category", source_value="X", target_category_idx="cat-a"
        )


def test_unique_constraint_category_mapping(pim_channel_factory, pim_category_factory):
    channel = pim_channel_factory("ch-1")
    pim_category_factory(channel, "cat-a")
    profile = MappingProfileFactory(target_channel_idxs=["ch-1"])
    mapping_service.add_category_mapping(profile, "category", "Shoes", "cat-a")

    with pytest.raises(IntegrityError):
        mapping_service.add_category_mapping(profile, "category", "Shoes", "cat-a")


# ---------------------------------------------------------------------------
# validate_profile (17-19)
# ---------------------------------------------------------------------------


def test_validate_profile_ok_for_correct_setup(pim_channel_factory, pim_feature_factory, pim_category_factory):
    channel = pim_channel_factory("ch-1")
    pim_feature_factory("feat-1")
    pim_category_factory(channel, "cat-1")
    source = SourceFactory()
    profile = mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1"])
    mapping_service.add_attribute_mapping(profile, "color", AttributeMappingTargetType.FEATURE.value, "feat-1")
    mapping_service.add_category_mapping(profile, "category", "X", "cat-1")

    result = mapping_service.validate_profile(source.idx, "p1")

    assert result["ok"] is True
    assert result["errors"] == []


def test_validate_profile_broken_after_feature_deleted(pim_channel_factory, pim_feature_factory):
    pim_channel_factory("ch-1")
    feature = pim_feature_factory("feat-1")
    source = SourceFactory()
    profile = mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1"])
    mapping_service.add_attribute_mapping(profile, "color", AttributeMappingTargetType.FEATURE.value, "feat-1")
    feature.delete()

    result = mapping_service.validate_profile(source.idx, "p1")

    assert result["ok"] is False
    assert any("feat-1" in err for err in result["errors"])


def test_validate_profile_no_mappings_warns(pim_channel_factory):
    pim_channel_factory("ch-1")
    source = SourceFactory()
    mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1"])

    result = mapping_service.validate_profile(source.idx, "p1")

    assert result["ok"] is True
    # warnings now structured list[dict] with `code`
    codes = [w["code"] for w in result["warnings"]]
    assert "no_mappings_configured" in codes


def test_validate_profile_warnings_use_mapping_warning_shape(pim_channel_factory):
    """contract smoke: every warning is a dict with the documented MappingWarning keys."""
    pim_channel_factory("ch-1")
    source = SourceFactory()
    mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1"])

    result = mapping_service.validate_profile(source.idx, "p1")

    assert isinstance(result["warnings"], list)
    assert result["warnings"], "expected at least one warning for an empty profile"
    sample = result["warnings"][0]
    assert {
        "code",
        "message",
        "mapping_kind",
        "mapping_id",
        "source_field",
        "source_value",
        "target_identifier",
        "details",
    } <= set(sample.keys())


# ---------------------------------------------------------------------------
# Misc (20-22)
# ---------------------------------------------------------------------------


def test_delete_profile_cascades_mappings(pim_channel_factory, pim_feature_factory):
    pim_channel_factory("ch-1")
    pim_feature_factory("feat-1")
    source = SourceFactory()
    profile = mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1"])
    mapping_service.add_attribute_mapping(profile, "x", AttributeMappingTargetType.FEATURE.value, "feat-1")

    mapping_service.delete_profile(source.idx, "p1")

    assert SourceMappingProfile.objects.filter(idx="p1").count() == 0
    assert SourceAttributeMapping.objects.filter(profile_id=profile.pk).count() == 0


def test_list_profiles_filters_per_source():
    s1 = SourceFactory()
    s2 = SourceFactory()
    MappingProfileFactory(source=s1, idx="p1")
    MappingProfileFactory(source=s1, idx="p2")
    MappingProfileFactory(source=s2, idx="p1")

    s1_profiles = mapping_service.list_profiles(s1.idx)
    assert s1_profiles.count() == 2


def test_get_profile_missing_raises():
    """C4: service raises ValueError (translated from DoesNotExist) for view layer."""
    source = SourceFactory()
    with pytest.raises(ValueError, match="not found"):
        mapping_service.get_profile(source.idx, "nope")


# ---------------------------------------------------------------------------
# Decision #24 — channel disjoint validation (23-24)
# ---------------------------------------------------------------------------


def test_channels_disjoint_ok_when_no_overlap(pim_channel_factory):
    pim_channel_factory("ch-1")
    pim_channel_factory("ch-2")
    pim_channel_factory("ch-3")
    source = SourceFactory()
    mapping_service.create_profile(source.idx, "pa", "PA", ["ch-1", "ch-2"])

    profile_b = mapping_service.create_profile(source.idx, "pb", "PB", ["ch-3"])

    assert profile_b.target_channel_idxs == ["ch-3"]


def test_channels_disjoint_collision_raises_and_inactive_bypasses(pim_channel_factory):
    pim_channel_factory("ch-1")
    pim_channel_factory("ch-2")
    pim_channel_factory("ch-3")
    source = SourceFactory()
    mapping_service.create_profile(source.idx, "pa", "PA", ["ch-1", "ch-2"])

    with pytest.raises(ValueError, match=r"\['ch-2'\] already covered"):
        mapping_service.create_profile(source.idx, "pb", "PB", ["ch-2", "ch-3"])

    inactive = mapping_service.create_profile(source.idx, "pb", "PB", ["ch-2", "ch-3"], is_active=False)
    assert inactive.is_active is False

    with pytest.raises(ValueError, match=r"already covered"):
        mapping_service.update_profile(source.idx, "pb", is_active=True)

    mapping_service.update_profile(source.idx, "pa", is_active=False)
    activated = mapping_service.update_profile(source.idx, "pb", is_active=True)
    assert activated.is_active is True


# ---------------------------------------------------------------------------
# Decision #26 — channel removal event (25)
# ---------------------------------------------------------------------------


def test_channel_removal_emits_event_with_affected_skus_count(pim_channel_factory):
    pim_channel_factory("ch-1")
    pim_channel_factory("ch-2")
    source = SourceFactory()
    profile = mapping_service.create_profile(source.idx, "p1", "P1", ["ch-1", "ch-2"])

    for _ in range(5):
        SourceProductFactory(source=source, status="pushed", pushed_to_channel_idxs=["ch-2"])

    mapping_service.update_profile(source.idx, profile.idx, target_channel_idxs=["ch-1"])

    event = IntegrationEvent.objects.filter(event_type=EventType.CHANNEL_REMOVED_FROM_PROFILE.value).get()
    assert event.severity == "warning"
    assert event.details["profile_idx"] == "p1"
    assert event.details["removed_channel_idxs"] == ["ch-2"]
    assert event.details["affected_skus_count"] == 5


@pytest.mark.django_db
def test_update_profile_rejects_unknown_field(pim_channel):
    source = SourceFactory()
    profile = mapping_service.create_profile(source.idx, "p-ms", "P", target_channel_idxs=[pim_channel.idx])
    with pytest.raises(ValueError, match="not editable"):
        mapping_service.update_profile(source.idx, profile.idx, created_at="2020-01-01")
