# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.core.validators import MinValueValidator
from django.db import models
from django_utils.models.base_model import BaseModel

from django_atlas.enums import EvalFrequency, PrimaryStrategy, ReviewMode, SourceKind, SourceType


class Source(BaseModel):
    idx = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    kind = models.CharField(max_length=20, choices=SourceKind.choices, default=SourceKind.PROCUREMENT)
    source_type = models.CharField(max_length=20, choices=SourceType.choices, default=SourceType.FEED)
    review_mode = models.CharField(max_length=10, choices=ReviewMode.choices, default=ReviewMode.MANUAL)
    is_active = models.BooleanField(default=True)
    is_trusted = models.BooleanField(
        default=True,
        help_text="Generic reliability flag. Downstream consumers (e.g. pricefighter) may filter to trusted-only sources.",
    )

    default_language = models.ForeignKey("django_regional.Language", on_delete=models.PROTECT, related_name="+")
    default_currency = models.ForeignKey("django_regional.Currency", on_delete=models.PROTECT, related_name="+")
    country = models.ForeignKey(
        "django_regional.Country", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    # Market context for monitoring/enrichment sources — null = source is global (not region-specific).
    # A per-region aggregator is modelled as a separate Source per region. Observations inherit this
    # context from their Source. Distinct from `default_currency` (feed's own currency, always required).
    currency = models.ForeignKey(
        "django_regional.Currency", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    sku_prefix = models.CharField(max_length=10, default="")
    default_feature_set_idx = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001 — FK-string reference, NULL = unconfigured
    target_warehouse_code = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001 — FK-string reference, NULL = unconfigured
    # PIM ProductClassEnum: 0=ProductBase, 1=ProductSimple, 2=ProductConfigurable, 3=ProductBundle.
    # Default 1 (Simple) — ProductBase was an opaque legacy default; storefront/baseline ship Simple.
    default_product_class = models.PositiveSmallIntegerField(default=1)

    # auto EAN-match config. Per-source knobs so niche sources with poor
    # physical data can widen the threshold or opt out entirely. See docs/auto-ean-match.md.
    realproduct_match_tolerance_pct = models.PositiveSmallIntegerField(
        default=10,
        validators=[MinValueValidator(0)],
        help_text="Max acceptable per-field diff (%) when EAN-matching an existing RealProduct.",
    )
    realproduct_match_strict = models.BooleanField(
        default=False, help_text="When True, missing physical field on either side fails tolerance (no skip)."
    )
    disable_ean_auto_link = models.BooleanField(
        default=False, help_text="When True, init push never tries EAN-based RealProduct lookup (per-source opt-out)."
    )

    # primary-only physical writes.
    # Default False: non-primary sources may NOT overwrite RealProduct physical fields
    # (weight/ean/width/height/deep). Opt-in True restores legacy last-write-wins for the
    # rare case where a non-primary source has vendor-tested measurements but loses on price.
    allow_physical_writes_from_non_primary = models.BooleanField(
        default=False,
        help_text=(
            "When True, this source's delta sync may overwrite RealProduct physical fields "
            "(weight, ean, width, height, deep) even if its link is not primary. Default "
            "False — primary is the single source of truth. Opt-in only for vendor-tested "
            "measurement use cases (audited as physical_update_overwrite warning event)."
        ),
    )

    # auto-primary selection config. Strategy chooses the winner,
    # cooldown/hysteresis are anti-flap guards. eval_frequency lets a high-velocity source
    # opt into hourly evaluation or skip cron entirely. See docs/primary-strategy.md.
    primary_strategy = models.CharField(
        max_length=32,
        choices=PrimaryStrategy.choices,
        default=PrimaryStrategy.LOWEST_COST_WITH_STOCK,
        help_text="Picker strategy for auto-primary evaluation across this source's links.",
    )
    primary_switch_cooldown_hours = models.PositiveIntegerField(
        default=24, help_text="Minimum hours between consecutive auto-primary switches on the same RealProduct."
    )
    primary_switch_hysteresis_pct = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(0)],
        help_text="Minimum cost-improvement (%) required before auto-primary swaps the winner.",
    )
    eval_frequency = models.CharField(
        max_length=16,
        choices=EvalFrequency.choices,
        default=EvalFrequency.DAILY,
        help_text="How often the auto-primary cron evaluates this source's RealProducts.",
    )

    qty_subtract = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    qty_minimum = models.IntegerField(default=0, validators=[MinValueValidator(0)])

    company_name = models.CharField(max_length=128, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=32, blank=True, default="")
    contact_person = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    lead_time_days = models.IntegerField(null=True, blank=True)

    credentials = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["idx"]
        indexes = [models.Index(fields=["idx"]), models.Index(fields=["is_active"]), models.Index(fields=["kind"])]

    def __str__(self) -> str:
        return self.name
