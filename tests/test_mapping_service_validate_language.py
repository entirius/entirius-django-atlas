# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""mapping_service.validate_profile language mismatch error.

When profile.import_language is NULL and source.default_language is not
declared by a target channel, validate must surface a hard error so the
operator must pick an override before push.
"""

import pytest
from django_regional.models.language import Language

from django_atlas.services import mapping_service
from tests.factories import MappingProfileFactory, SourceFactory

pytestmark = pytest.mark.django_db


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
def channel_en_only(pim_channel_factory, lang_en):
    return pim_channel_factory("ch-en-only", "EN-only Channel")


@pytest.fixture
def channel_pl_en(pim_channel_factory, lang_pl, lang_en):
    ch = pim_channel_factory("ch-pl-en", "PL+EN Channel")
    ch.languages.add(lang_pl)
    return ch


def test_validate_profile_reports_language_mismatch_when_no_override(lang_pl, channel_en_only):
    """PL source + EN-only channel + no override → error with stable [profile_language_mismatch] prefix."""
    sup = SourceFactory(idx="sup-pl-mismatch", default_language=lang_pl)
    profile = MappingProfileFactory(
        source=sup, idx="prof-mismatch", import_language=None, target_channel_idxs=[channel_en_only.idx]
    )
    result = mapping_service.validate_profile("sup-pl-mismatch", profile.idx)
    assert result["ok"] is False
    mismatches = [e for e in result["errors"] if e.startswith("[profile_language_mismatch]")]
    assert len(mismatches) == 1
    assert channel_en_only.idx in mismatches[0]
    assert "default_language='pl'" in mismatches[0]


def test_validate_profile_passes_when_source_lang_in_channel_languages(lang_pl, channel_pl_en):
    """PL source + channel that supports PL → no language error."""
    sup = SourceFactory(idx="sup-pl-ok", default_language=lang_pl)
    profile = MappingProfileFactory(
        source=sup, idx="prof-ok", import_language=None, target_channel_idxs=[channel_pl_en.idx]
    )
    result = mapping_service.validate_profile("sup-pl-ok", profile.idx)
    mismatches = [e for e in result["errors"] if e.startswith("[profile_language_mismatch]")]
    assert mismatches == []


def test_validate_profile_passes_when_import_language_override_set(lang_pl, lang_en, channel_en_only):
    """PL source + EN-only channel + explicit import_language=en → no language error."""
    sup = SourceFactory(idx="sup-pl-override", default_language=lang_pl)
    profile = MappingProfileFactory(
        source=sup, idx="prof-override", import_language=lang_en, target_channel_idxs=[channel_en_only.idx]
    )
    result = mapping_service.validate_profile("sup-pl-override", profile.idx)
    mismatches = [e for e in result["errors"] if e.startswith("[profile_language_mismatch]")]
    assert mismatches == []
