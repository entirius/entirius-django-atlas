# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for value_transformer."""

from decimal import Decimal

from django_atlas.services import value_transformer


def test_none_modifier_returns_value_unchanged():
    result = value_transformer.transform(7800, "none")
    assert result.value == 7800
    assert result.applied is False
    assert result.failure_reason is None


def test_none_value_returns_none_regardless_of_modifier():
    result = value_transformer.transform(None, "grams_to_kg")
    assert result.value is None
    assert result.applied is False


def test_grams_to_kg_int():
    result = value_transformer.transform(7800, "grams_to_kg")
    assert result.value == Decimal("7.8")
    assert result.applied is True


def test_grams_to_kg_string_numeric():
    result = value_transformer.transform("7800", "grams_to_kg")
    assert result.value == Decimal("7.8")
    assert result.applied is True


def test_kg_to_grams_decimal():
    result = value_transformer.transform(Decimal("7.8"), "kg_to_grams")
    assert result.value == Decimal("7800.0")
    assert result.applied is True


def test_mm_to_cm():
    result = value_transformer.transform(100, "mm_to_cm")
    assert result.value == Decimal("10")
    assert result.applied is True


def test_cm_to_mm():
    result = value_transformer.transform(10, "cm_to_mm")
    assert result.value == Decimal("100")
    assert result.applied is True


def test_mm_to_m():
    result = value_transformer.transform(1500, "mm_to_m")
    assert result.value == Decimal("1.5")
    assert result.applied is True


def test_currency_minor_to_major():
    result = value_transformer.transform(2999, "currency_minor_to_major")
    assert result.value == Decimal("29.99")
    assert result.applied is True


def test_currency_major_to_minor():
    result = value_transformer.transform(Decimal("29.99"), "currency_major_to_minor")
    assert result.value == Decimal("2999.00")
    assert result.applied is True


def test_string_trim():
    result = value_transformer.transform("  Hello world  ", "string_trim")
    assert result.value == "Hello world"
    assert result.applied is True


def test_string_lowercase():
    result = value_transformer.transform("BLUE", "string_lowercase")
    assert result.value == "blue"
    assert result.applied is True


def test_string_uppercase():
    result = value_transformer.transform("blue", "string_uppercase")
    assert result.value == "BLUE"
    assert result.applied is True


def test_numeric_modifier_on_garbage_string_returns_raw_with_reason():
    result = value_transformer.transform("not-a-number", "grams_to_kg")
    assert result.value == "not-a-number"
    assert result.applied is False
    assert result.failure_reason == "invalid_decimal"


def test_string_modifier_on_int_returns_raw_with_reason():
    result = value_transformer.transform(123, "string_trim")
    assert result.value == 123
    assert result.applied is False
    assert result.failure_reason == "type_mismatch"


def test_unknown_modifier_returns_raw_with_reason():
    result = value_transformer.transform(10, "feet_to_meters")
    assert result.value == 10
    assert result.applied is False
    assert result.failure_reason == "unknown_modifier"


def test_bool_is_rejected_as_numeric():
    """Pythonic Decimal(str(True)) works but is almost always a mapping bug — reject."""
    result = value_transformer.transform(True, "grams_to_kg")
    assert result.value is True
    assert result.applied is False
    assert result.failure_reason == "invalid_decimal"
