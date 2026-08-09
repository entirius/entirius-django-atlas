# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""language resolution against channel context + fallback warning emit.

Covers:
  - resolve_language_for_channel hierarchy (override / source-default / fallback)
  - LanguageResolution dataclass diagnostics
  - legacy resolve_language() shim parity
  - init_push_to_channel + force_repush_to_channel:
      * value_txt_t9n[fallback_lang] is populated when source lang ∉ channel
      * IntegrationEvent 'language_fallback' written
      * SourceProductChangeLog row 'language_fallback' written
      * event_sink list mutated with one entry per fallback
"""

from decimal import Decimal

import pytest
from django_regional.models.language import Language

from django_atlas.enums import ChangeLogSource, EventSeverity, EventType, ProductStatus
from django_atlas.models import AttributeMappingTargetType, IntegrationEvent, SourceProductChangeLog
from django_atlas.services import pim_writer
from django_atlas.services.pim_writer import (
    LANG_SOURCE_CHANNEL_DEFAULT_FALLBACK,
    LANG_SOURCE_PROFILE_OVERRIDE,
    LANG_SOURCE_SOURCE_DEFAULT,
)
from tests.factories import (
    AttributeMappingFactory,
    FeedFactory,
    MappingProfileFactory,
    SourceFactory,
    SourceProductFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers — language fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lang_en(db) -> Language:
    obj, _ = Language.objects.get_or_create(
        iso2="en", defaults={"iso3": "eng", "name_en": "English", "name_pl": "angielski"}
    )
    return obj


@pytest.fixture
def lang_pl(db) -> Language:
    obj, _ = Language.objects.get_or_create(
        iso2="pl", defaults={"iso3": "pol", "name_en": "Polish", "name_pl": "polski"}
    )
    return obj


@pytest.fixture
def channel_en_only(db, pim_channel_factory, lang_en):
    """Channel default+only language = EN. Channel.save() auto-adds default_language to languages M2M."""
    return pim_channel_factory("ch-en-only", "EN-only Channel")


@pytest.fixture
def channel_pl_en(db, pim_channel_factory, lang_pl, lang_en):
    """Channel with default=EN but languages={EN, PL}."""
    ch = pim_channel_factory("ch-pl-en", "PL+EN Channel")
    ch.languages.add(lang_pl)
    return ch


# ---------------------------------------------------------------------------
# Unit — resolve_language_for_channel
# ---------------------------------------------------------------------------


def test_resolve_language_for_channel_profile_override_wins(lang_en, lang_pl, channel_en_only):
    """Profile.import_language=PL overrides channel context — operator's explicit choice."""
    sup = SourceFactory(default_language=lang_pl)
    profile = MappingProfileFactory(source=sup, import_language=lang_pl)
    res = pim_writer.resolve_language_for_channel(profile, sup, channel_en_only)
    assert res.language == "pl"
    assert res.source == LANG_SOURCE_PROFILE_OVERRIDE
    assert res.used_fallback is False
    # Diagnostics populated even on override.
    assert res.source_language == "pl"
    assert res.channel_default_language == "en"
    assert "en" in res.channel_languages


def test_resolve_language_for_channel_source_default_in_channel_languages(lang_en, lang_pl, channel_pl_en):
    """Source PL pushed to channel that supports PL → use source language, no fallback."""
    sup = SourceFactory(default_language=lang_pl)
    profile = MappingProfileFactory(source=sup, import_language=None)
    res = pim_writer.resolve_language_for_channel(profile, sup, channel_pl_en)
    assert res.language == "pl"
    assert res.source == LANG_SOURCE_SOURCE_DEFAULT
    assert res.used_fallback is False


def test_resolve_language_for_channel_fallback_to_channel_default(lang_en, lang_pl, channel_en_only):
    """Source PL pushed to EN-only channel without override → fallback to EN + flag."""
    sup = SourceFactory(default_language=lang_pl)
    profile = MappingProfileFactory(source=sup, import_language=None)
    res = pim_writer.resolve_language_for_channel(profile, sup, channel_en_only)
    assert res.language == "en"
    assert res.source == LANG_SOURCE_CHANNEL_DEFAULT_FALLBACK
    assert res.used_fallback is True


def test_resolve_language_for_channel_dataclass_diagnostics(lang_en, lang_pl, channel_pl_en):
    """LanguageResolution carries enough context for audit row + IntegrationEvent details."""
    sup = SourceFactory(default_language=lang_pl)
    profile = MappingProfileFactory(source=sup, import_language=None)
    res = pim_writer.resolve_language_for_channel(profile, sup, channel_pl_en)
    assert res.source_language == "pl"
    assert res.channel_default_language == "en"
    assert sorted(res.channel_languages) == ["en", "pl"]
    # Frozen dataclass → asdict() round-trip stable for JSON storage.
    from dataclasses import asdict

    payload = asdict(res)
    assert payload["language"] == "pl"
    assert payload["source"] == LANG_SOURCE_SOURCE_DEFAULT


def test_legacy_resolve_language_still_works(lang_en, lang_pl):
    """Two-arg resolve_language() backward-compat shim — existing callers stay green."""
    sup = SourceFactory(default_language=lang_pl)
    profile_override = MappingProfileFactory(source=sup, import_language=lang_en)
    profile_default = MappingProfileFactory(source=sup, import_language=None, idx="profile-default")
    assert pim_writer.resolve_language(profile_override, sup) == "en"
    assert pim_writer.resolve_language(profile_default, sup) == "pl"


# ---------------------------------------------------------------------------
# Integration — init_push_to_channel + force_repush_to_channel emit fallback
# ---------------------------------------------------------------------------


def _register_feature_in_feature_set(feature, feature_set):
    from django_pim.models.feature_set import FeatureInFeatureSet

    FeatureInFeatureSet.objects.get_or_create(feature=feature, feature_set=feature_set)


def _setup_pl_to_en_push(*, admin_user, lang_pl, lang_en, channel, pim_feature_set_factory, pim_typed_feature_factory):
    """Common setup: PL source + EN channel + one TEXT_T9N feature mapping."""
    from django_pim.models.feature import FeatureTypeEnum

    feature_set = pim_feature_set_factory("fs-lang")
    name_feature = pim_typed_feature_factory("lang-test-name", FeatureTypeEnum.TEXT_T9N)
    _register_feature_in_feature_set(name_feature, feature_set)

    sup = SourceFactory(idx="sup-pl", default_language=lang_pl, default_feature_set_idx="fs-lang")
    feed = FeedFactory(source=sup)
    profile = MappingProfileFactory(
        source=sup, idx="prof-lang", import_language=None, target_channel_idxs=[channel.idx]
    )
    AttributeMappingFactory(
        profile=profile,
        source_field=pim_writer._TOKEN_NAME,
        target_type=AttributeMappingTargetType.FEATURE.value,
        target_identifier="lang-test-name",
    )
    sp = SourceProductFactory(
        source=sup,
        feed=feed,
        external_id="LANG-001",
        name="Foliopak 450x550",
        cost=Decimal("12.50"),
        ean="5901234567890",
        status=ProductStatus.APPROVED.value,
    )
    return sp, profile, sup


def test_init_push_to_channel_with_language_fallback_emits_event_and_audit_row(
    admin_user, lang_pl, lang_en, channel_en_only, pim_feature_set_factory, pim_typed_feature_factory
):
    sp, profile, source = _setup_pl_to_en_push(
        admin_user=admin_user,
        lang_pl=lang_pl,
        lang_en=lang_en,
        channel=channel_en_only,
        pim_feature_set_factory=pim_feature_set_factory,
        pim_typed_feature_factory=pim_typed_feature_factory,
    )

    sink: list[dict] = []
    product = pim_writer.init_push_to_channel(sp, profile, channel_en_only.idx, admin_user, event_sink=sink)

    # (a) value_txt_t9n.en populated (NOT .pl) — the actual language-resolution fix.
    from django_pim.models.product_attribute import ProductAttribute

    attr = ProductAttribute.objects.get(product=product, feature__idx="lang-test-name")
    assert attr.value_txt_t9n.get("en") == "Foliopak 450x550"
    assert "pl" not in attr.value_txt_t9n

    # (b) IntegrationEvent persisted.
    events = IntegrationEvent.objects.filter(event_type=EventType.LANGUAGE_FALLBACK.value)
    assert events.count() == 1
    ev = events.first()
    assert ev.severity == EventSeverity.WARNING.value
    assert ev.source_id == source.id
    assert ev.source_product_id == sp.id
    assert ev.details["channel_idx"] == channel_en_only.idx
    assert ev.details["source_language"] == "pl"
    assert ev.details["resolved_language"] == "en"

    # (c) audit log row exists with the dedicated source.
    audit_rows = SourceProductChangeLog.objects.filter(
        source_product=sp, source=ChangeLogSource.LANGUAGE_FALLBACK.value
    )
    assert audit_rows.count() == 1
    row = audit_rows.first()
    assert row.field_path == f"language_resolution.{channel_en_only.idx}"
    assert row.before is None
    assert row.after["language"] == "en"
    assert row.after["used_fallback"] is True
    assert row.real_product_sku  # populated since RealProduct created during init_push

    # (d) sink got one entry.
    assert len(sink) == 1
    assert sink[0]["event_type"] == EventType.LANGUAGE_FALLBACK.value
    assert sink[0]["severity"] == EventSeverity.WARNING.value
    assert "channel_idx" in sink[0]["details"]


def test_force_repush_to_channel_re_emits_language_fallback_when_still_mismatched(
    admin_user, lang_pl, lang_en, channel_en_only, pim_feature_set_factory, pim_typed_feature_factory
):
    sp, profile, source = _setup_pl_to_en_push(
        admin_user=admin_user,
        lang_pl=lang_pl,
        lang_en=lang_en,
        channel=channel_en_only,
        pim_feature_set_factory=pim_feature_set_factory,
        pim_typed_feature_factory=pim_typed_feature_factory,
    )

    # First push — fallback fires once.
    pim_writer.init_push_to_channel(sp, profile, channel_en_only.idx, admin_user)
    sp.refresh_from_db()
    sp.status = ProductStatus.PUSHED.value
    sp.save(update_fields=["status", "modified_at"])
    assert IntegrationEvent.objects.filter(event_type=EventType.LANGUAGE_FALLBACK.value).count() == 1

    # Force re-push with mismatched language still in place — should re-fire.
    sink: list[dict] = []
    pim_writer.force_repush_to_channel(sp, profile, channel_en_only.idx, admin_user, event_sink=sink)
    assert IntegrationEvent.objects.filter(event_type=EventType.LANGUAGE_FALLBACK.value).count() == 2
    audit_rows = SourceProductChangeLog.objects.filter(source=ChangeLogSource.LANGUAGE_FALLBACK.value)
    assert audit_rows.count() == 2
    assert len(sink) == 1


def test_push_without_fallback_does_not_emit_event(
    admin_user, lang_pl, lang_en, channel_pl_en, pim_feature_set_factory, pim_typed_feature_factory
):
    """Happy path: source PL pushed to a channel that supports PL → no warning."""
    sp, profile, _ = _setup_pl_to_en_push(
        admin_user=admin_user,
        lang_pl=lang_pl,
        lang_en=lang_en,
        channel=channel_pl_en,
        pim_feature_set_factory=pim_feature_set_factory,
        pim_typed_feature_factory=pim_typed_feature_factory,
    )

    sink: list[dict] = []
    pim_writer.init_push_to_channel(sp, profile, channel_pl_en.idx, admin_user, event_sink=sink)

    assert IntegrationEvent.objects.filter(event_type=EventType.LANGUAGE_FALLBACK.value).count() == 0
    assert SourceProductChangeLog.objects.filter(source=ChangeLogSource.LANGUAGE_FALLBACK.value).count() == 0
    assert sink == []
