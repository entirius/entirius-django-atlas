# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stateless value transformer for SourceAttributeMapping.modifier.

KISS: single modifier per mapping row, no chaining, no custom expressions.
Numeric modifiers operate on Decimal (precision preserved, no float drift).
String modifiers operate on str. Type mismatches return the raw value with
a failure_reason — never raises, so the push pipeline cannot crash on bad data.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django_atlas.enums import MAPPING_VALUE_MODIFIERS, MappingValueModifier

_K = Decimal("1000")
_HUNDRED = Decimal("100")
_TEN = Decimal("10")


@dataclass(frozen=True)
class TransformResult:
    """Carries both the value and metadata about whether the transform fired."""

    value: Any
    applied: bool
    failure_reason: str | None = None


def transform(value: Any, modifier: str | None) -> TransformResult:
    """Apply modifier to value. Robust: never raises, returns raw on type mismatch."""
    if value is None or modifier in (None, "", MappingValueModifier.NONE.value):
        return TransformResult(value=value, applied=False)
    if modifier not in MAPPING_VALUE_MODIFIERS:
        return TransformResult(value=value, applied=False, failure_reason="unknown_modifier")
    handler = _DISPATCH[modifier]
    return handler(value)


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _numeric(value: Any, factor: Decimal, *, divide: bool) -> TransformResult:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return TransformResult(value=value, applied=False, failure_reason="invalid_decimal")
    transformed = decimal_value / factor if divide else decimal_value * factor
    return TransformResult(value=transformed, applied=True)


def _string(value: Any, op) -> TransformResult:
    if not isinstance(value, str):
        return TransformResult(value=value, applied=False, failure_reason="type_mismatch")
    return TransformResult(value=op(value), applied=True)


_DISPATCH = {
    MappingValueModifier.GRAMS_TO_KG.value: lambda v: _numeric(v, _K, divide=True),
    MappingValueModifier.KG_TO_GRAMS.value: lambda v: _numeric(v, _K, divide=False),
    MappingValueModifier.MM_TO_CM.value: lambda v: _numeric(v, _TEN, divide=True),
    MappingValueModifier.CM_TO_MM.value: lambda v: _numeric(v, _TEN, divide=False),
    MappingValueModifier.MM_TO_M.value: lambda v: _numeric(v, _K, divide=True),
    MappingValueModifier.CURRENCY_MINOR_TO_MAJOR.value: lambda v: _numeric(v, _HUNDRED, divide=True),
    MappingValueModifier.CURRENCY_MAJOR_TO_MINOR.value: lambda v: _numeric(v, _HUNDRED, divide=False),
    MappingValueModifier.STRING_TRIM.value: lambda v: _string(v, str.strip),
    MappingValueModifier.STRING_LOWERCASE.value: lambda v: _string(v, str.lower),
    MappingValueModifier.STRING_UPPERCASE.value: lambda v: _string(v, str.upper),
}
