# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Auto EAN-match.

Resolves whether a `SourceProduct` should auto-link to an existing PIM `RealProduct`
based on EAN equality + physical-tolerance safety check. `pim_writer.init_push_to_channel`
calls into here BEFORE its `RealProduct.get_or_create(sku=...)` to short-circuit the
per-source-prefixed SKU and reuse the existing RealProduct instead — that's how
`multi_source_overlap` finally becomes possible in the natural flow.

The split into two functions keeps each one testable in isolation:
  - `find_match_by_ean` — pure DB lookup with opt-out + empty-EAN guards
  - `physical_tolerance_check` — pure compare (no DB), takes the candidate defaults
    dict that `_real_product_defaults` already built so we don't recompute or touch ORM

Failure modes are documented per-branch in `docs/auto-ean-match.md`.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django_atlas.models import Source, SourceProduct

# Compared physical fields. Decimal in both SP-side defaults and RealProduct columns.
_PHYSICAL_FIELDS: tuple[str, ...] = ("weight", "width", "height", "deep")

# Floor used in the diff-pct denominator so a 0.000 vs 0.001 compare doesn't blow up.
# Pick a value smaller than any realistic physical attribute (sub-mg / sub-mm).
_DIFF_PCT_FLOOR: Decimal = Decimal("0.001")


@dataclass
class ToleranceResult:
    """Outcome of physical-tolerance check between candidate SP fields and existing RealProduct.

    - `passed=True` → safe to auto-link.
    - `failed_fields` → fields that exceed `Source.realproduct_match_tolerance_pct` OR
      (strict mode) are missing on either side.
    - `skipped_fields` → fields excluded from comparison (non-strict, missing on either side).
    - `diffs_pct` → per-field computed diff for fields that were actually compared.
    """

    passed: bool
    failed_fields: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    diffs_pct: dict[str, Decimal] = field(default_factory=dict)


def find_match_by_ean(sp: "SourceProduct", source: "Source") -> Any | None:
    """Return the first RealProduct sharing EAN with `sp`, or None.

    Short-circuits on opt-out (`source.disable_ean_auto_link`) and empty EAN.
    Uses the `RealProduct.ean` DB index. `.order_by("id").first()` makes the choice
    deterministic when the DB already has duplicates (operator can untangle later
    with `manage.py find_duplicate_realproducts --by ean`).
    """
    if source.disable_ean_auto_link:
        return None
    ean = (sp.ean or "").strip()
    if not ean:
        return None
    from django_pim.models.real_product import RealProduct

    return RealProduct.objects.filter(ean=ean).order_by("id").first()


def _coerce_decimal(value: Any) -> Decimal | None:
    """Decimal-or-None coercion. Defensive — `_real_product_defaults` already coerces,
    but the existing_rp side comes straight from ORM (Decimal or None already)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _diff_pct(new: Decimal, existing: Decimal) -> Decimal:
    """abs(new - existing) / max(abs(new), abs(existing), floor) * 100."""
    denom = max(abs(new), abs(existing), _DIFF_PCT_FLOOR)
    return (abs(new - existing) / denom) * Decimal("100")


def physical_tolerance_check(rp_defaults: dict[str, Any], existing_rp: Any, source: "Source") -> ToleranceResult:
    """Compare weight/width/height/deep between candidate SP defaults and the existing RealProduct.

    Strict mode (`source.realproduct_match_strict=True`):
        Missing on either side → field counted as failed.
    Non-strict (default):
        Missing on either side → field skipped (not failed).

    For fields where both sides have a value, computes `diff_pct` and fails the field if it
    exceeds `source.realproduct_match_tolerance_pct`. Result.passed is True iff no failures.
    """
    tolerance_pct = Decimal(source.realproduct_match_tolerance_pct)
    strict = bool(source.realproduct_match_strict)

    failed: list[str] = []
    skipped: list[str] = []
    diffs: dict[str, Decimal] = {}

    for field_name in _PHYSICAL_FIELDS:
        new_val = _coerce_decimal(rp_defaults.get(field_name))
        existing_val = _coerce_decimal(getattr(existing_rp, field_name, None))
        if new_val is None or existing_val is None:
            if strict:
                failed.append(field_name)
            else:
                skipped.append(field_name)
            continue
        diff_pct = _diff_pct(new_val, existing_val)
        diffs[field_name] = diff_pct
        if diff_pct > tolerance_pct:
            failed.append(field_name)

    return ToleranceResult(passed=not failed, failed_fields=failed, skipped_fields=skipped, diffs_pct=diffs)
