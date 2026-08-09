# Mapping value modifiers

`SourceAttributeMapping.modifier` lets the operator declare a one-shot unit
conversion or string normalisation that runs on the source value before it is
written to PIM. Single modifier per row, no chaining, no custom expressions —
KISS is the whole point.

Implemented in `services.value_transformer.transform(value, modifier)`. Robust:
the function never raises. On type mismatch the raw value is returned with a
`failure_reason`, a warning `IntegrationEvent(event_type=mapping_transform_failed)`
is emitted, and the field is skipped — the push pipeline cannot crash on bad
mapping config.

## Choices

| Modifier | Operation | Input → Output | Use when |
|---|---|---|---|
| `none` | — (default) | `7800 → 7800` | No transform. |
| `grams_to_kg` | `/1000` (Decimal) | `7800 → 7.8` | Source ships `weight_g`; PIM `RealProduct.weight` is in kg. Scenario 6c. |
| `kg_to_grams` | `×1000` | `7.8 → 7800` | Inverse, rare. |
| `mm_to_cm` | `/10` | `100 → 10` | Source ships `length_mm`; PIM stores cm. |
| `cm_to_mm` | `×10` | `10 → 100` | Inverse. |
| `mm_to_m` | `/1000` | `1500 → 1.5` | Long-side dimensions. |
| `currency_minor_to_major` | `/100` | `2999 → 29.99` | Source ships price in cents/grosze; PIM stores major unit. |
| `currency_major_to_minor` | `×100` | `29.99 → 2999` | Inverse. |
| `string_trim` | `str.strip` | `"  abc  " → "abc"` | Whitespace-polluted source values. |
| `string_lowercase` | `str.lower` | `"BLUE" → "blue"` | Normalise feature lookup keys. |
| `string_uppercase` | `str.upper` | `"blue" → "BLUE"` | Normalise feature lookup keys. |

Numeric modifiers operate on `Decimal` — precision preserved, no float drift.
The handler coerces `int / float / str / Decimal` via `Decimal(str(value))`;
`bool` is rejected because `Decimal(str(True)) == 1` is almost always a mapping
bug. String modifiers require `isinstance(value, str)` — anything else returns
raw + `failure_reason="type_mismatch"`.

## Audit + observability

Every successful transform on a persisted field emits one
`SourceProductChangeLog` row with:

- `source = mapping_transform`
- `field_path = real_product.{field}` or `feature.{feature_idx}`
- `before` = raw source value (before transform)
- `after` = transformed value (what was actually written to PIM)

The audit row is only written when the field actually changes (e.g. INIT push
skips when `current not in (None, "")` and `force_overwrite=False`). Failures
emit a warning `IntegrationEvent` instead — operator sees the misconfiguration
in the events panel and the push continues with the field skipped.

## Adding a new modifier

1. Add a value to `enums.MappingValueModifier`.
2. Add a handler entry in `_DISPATCH` in `value_transformer.py`. Reuse `_numeric()`
   for unit conversions and `_string()` for string ops; only write custom code
   when neither fits.
3. Add a migration `AlterField` on `SourceAttributeMapping.modifier` (mirror
   `0010_etap_09_mapping_modifier.py`).
4. Add CMS i18n keys `sources.mappings.modifier.{key}` in `en.json` + `pl.json`.
5. Add a unit test in `tests/test_value_transformer.py`.
