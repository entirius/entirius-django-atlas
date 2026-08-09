# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.db import models


class SourceKind(models.TextChoices):
    """Discriminator for the ingestion engine. procurement=cost/stock feed (was 'trade'),
    monitoring=competitor price watch (read-only), enrichment=signal feed (was 'data')."""

    PROCUREMENT = "procurement", "Procurement"
    MONITORING = "monitoring", "Monitoring"
    ENRICHMENT = "enrichment", "Enrichment"


class SourceType(models.TextChoices):
    FEED = "feed", "Feed"
    MANUAL = "manual", "Manual"
    DROPSHIP = "dropship", "Dropship"


class ReviewMode(models.TextChoices):
    MANUAL = "manual", "Manual"
    AUTO = "auto", "Auto"


class EventSeverity(models.TextChoices):
    CRITICAL = "critical", "Critical"
    WARNING = "warning", "Warning"
    INFO = "info", "Info"


class EventType(models.TextChoices):
    FEED_FAILED = "feed_failed", "Feed failed"
    SCRAPER_BANNED = "scraper_banned", "Scraper banned"
    SCRAPER_DISPATCH_SKIPPED = "scraper_dispatch_skipped", "Scraper dispatch skipped"
    IMAGE_FAILED = "image_failed", "Image failed"
    PUSH_FAILED = "push_failed", "Push failed"
    PUSH_SUCCEEDED = "push_succeeded", "Push succeeded"
    COST_UPDATED = "cost_updated", "Cost updated"
    MASS_DELISTING = "mass_delisting", "Mass delisting"
    FEED_NO_RUN = "feed_no_run", "Feed no run"
    PIM_FEATURE_MISSING = "pim_feature_missing", "PIM feature missing"
    QMS_NOT_INSTALLED_SKIPPED = "qms_not_installed_skipped", "QMS not installed (skipped)"
    QMS_WAREHOUSE_NOT_CONFIGURED = "qms_warehouse_not_configured", "QMS warehouse not configured"
    QMS_WRITE_FAILED = "qms_write_failed", "QMS write failed"
    PUSHED_PRODUCT_DELISTED = "pushed_product_delisted", "Pushed product delisted"
    EAN_COLLISION_DETECTED = "ean_collision_detected", "EAN collision detected"
    UNKNOWN_EXTERNAL_ID_IN_DELTA = "unknown_external_id_in_delta", "Unknown external_id in delta"
    PHYSICAL_UPDATE_APPLIED = "physical_update_applied", "Physical update applied"
    FORCE_REPUSH_EXECUTED = "force_repush_executed", "Force re-push executed"
    IMPORT_COMPLETED = "import_completed", "Import completed"
    SOURCE_DELETED = "source_deleted", "Source deleted"
    CHANNEL_REMOVED_FROM_PROFILE = "channel_removed_from_profile", "Channel removed from profile"
    ATTRIBUTE_VALUE_MISSING = "attribute_value_missing", "Attribute value missing"
    PIM_ATTRIBUTE_MISSING = "pim_attribute_missing", "PIM attribute missing"
    PIM_CATEGORY_MISSING = "pim_category_missing", "PIM category missing"
    PIM_FEATURE_SET_MISSING = "pim_feature_set_missing", "PIM feature_set missing"
    PIM_CHANNEL_MISSING = "pim_channel_missing", "PIM channel missing"
    IMAGE_DOWNLOAD_STARTED = "image_download_started", "Image download started"
    IMAGE_DOWNLOAD_COMPLETED = "image_download_completed", "Image download completed"
    MULTI_SOURCE_OVERLAP = "multi_source_overlap", "Multi-source overlap"
    STOCK_UPDATED = "stock_updated", "Stock updated"
    QMS_NO_REAL_PRODUCT = "qms_no_real_product", "QMS no real_product"
    COST_MISSING = "cost_missing", "Cost missing"
    CURRENCY_MISSING = "currency_missing", "Currency missing"
    SCRAPER_RESULTS_VALIDATION_ERRORS = "scraper_results_validation_errors", "Scraper results validation errors"
    SCRAPER_UNKNOWN_RUN_ID = "scraper_unknown_run_id", "Scraper unknown run_id"
    SCRAPER_CALLBACK_SIGNATURE_INVALID = "scraper_callback_signature_invalid", "Scraper callback signature invalid"
    SOURCE_CREDENTIALS_VIEWED = "source_credentials_viewed", "Source credentials viewed"
    SOURCE_CREDENTIALS_UPDATED = "source_credentials_updated", "Source credentials updated"
    LANGUAGE_FALLBACK = "language_fallback", "Language fallback"
    MAPPING_TRANSFORM_FAILED = "mapping_transform_failed", "Mapping transform failed"
    # auto EAN-match in pim_writer.init_push_to_channel
    AUTO_LINKED_TO_EXISTING_REALPRODUCT = "auto_linked_to_existing_realproduct", "Auto-linked to existing RealProduct"
    PHYSICAL_TOLERANCE_VIOLATION = "physical_tolerance_violation", "Physical tolerance violation"
    MANUAL_UNLINK_FROM_REALPRODUCT = "manual_unlink_from_realproduct", "Manual unlink from RealProduct"
    # auto-primary selection
    PRIMARY_SOURCE_SWITCHED = "primary_source_switched", "Primary source switched"
    PRIMARY_SOURCE_EMERGENCY_SWITCH = "primary_source_emergency_switch", "Primary source emergency switch"
    PRIMARY_SOURCE_FORCED = "primary_source_forced", "Primary source forced (manual override)"
    PRIMARY_SWITCH_SKIPPED_COOLDOWN = "primary_switch_skipped_cooldown", "Primary switch skipped (cooldown)"
    PRIMARY_SWITCH_SKIPPED_HYSTERESIS = "primary_switch_skipped_hysteresis", "Primary switch skipped (hysteresis)"
    PRIMARY_SWITCH_SKIPPED_MANUAL_OVERRIDE = (
        "primary_switch_skipped_manual_override",
        "Primary switch skipped (manual override)",
    )
    # cross-source merge UI
    REALPRODUCT_MANUALLY_MERGED = "realproduct_manually_merged", "RealProduct manually merged"
    # multi-source physical race detection
    PHYSICAL_UPDATE_SKIPPED_NON_PRIMARY = (
        "physical_update_skipped_non_primary",
        "Physical update skipped (non-primary)",
    )
    PHYSICAL_UPDATE_OVERWRITE = "physical_update_overwrite", "Physical update overwrite (opt-in non-primary)"


class PrimaryStrategy(models.TextChoices):
    """Auto-primary source picker strategy.

    Only `lowest_cost_with_stock` is implemented in others reserved
    for future stages (highest_stock for premium-inventory routing, manual_only
    to opt a source out of auto-eval entirely).
    """

    LOWEST_COST_WITH_STOCK = "lowest_cost_with_stock", "Lowest cost with stock"
    HIGHEST_STOCK = "highest_stock", "Highest stock"
    MANUAL_ONLY = "manual_only", "Manual only"


class EvalFrequency(models.TextChoices):
    """How often the auto-primary cron evaluates a source's RealProducts.

    `daily` matches the default celery beat schedule (03:00 UTC). `hourly` is
    reserved for high-velocity catalogues. `manual` opts the source out of
    cron entirely — inline triggers (init_push, emergency stock=0) still fire.
    """

    DAILY = "daily", "Daily"
    HOURLY = "hourly", "Hourly"
    MANUAL = "manual", "Manual"


class PrimarySkipReason(models.TextChoices):
    """Structured reason taxonomy for evaluate_primary_source outcomes.

    `none` = candidate selected and applied. Other values explain why the
    evaluator did not swap the current primary. The operator can filter
    `SourceProductChangeLog` / `IntegrationEvent` by reason to triage
    'why did storefront price not change overnight'.
    """

    NONE = "none", "None"
    NO_CHANGE = "no_change", "No change (winner == current)"
    COOLDOWN = "cooldown", "Cooldown active"
    HYSTERESIS = "hysteresis", "Hysteresis threshold not exceeded"
    MANUAL_OVERRIDE = "manual_override", "Manual override sticky"
    NO_CANDIDATES = "no_candidates", "No candidates with stock"


class SyncMode(models.TextChoices):
    FULL = "full", "Full"
    DELTA = "delta", "Delta"


class ProductStatus(models.TextChoices):
    NEW = "new", "New"
    QUEUED = "queued", "Queued"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    PUSHED_PENDING_IMAGES = "pushed_pending_images", "Pushed (pending images)"
    PUSHED = "pushed", "Pushed"


class LogStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class LogMode(models.TextChoices):
    FULL = "full", "Full"
    DELTA = "delta", "Delta"
    TEST = "test", "Test"


class LogSource(models.TextChoices):
    CLI = "cli", "CLI"
    API = "api", "API"
    SCHEDULER = "scheduler", "Scheduler"
    SIGNAL = "signal", "Signal"


class ChangeLogSource(models.TextChoices):
    """SourceProductChangeLog.source whitelist. Schema-first: includes auto-link / auto-primary
    values from day one so future stages add emit calls without touching the enum."""

    FULL_SYNC = "full_sync", "Full sync"
    DELTA_SYNC = "delta_sync", "Delta sync"
    INIT_PUSH = "init_push", "Init push"
    FORCE_REPUSH = "force_repush", "Force re-push"
    OPERATOR_SP_EDIT = "operator_sp_edit", "Operator SP edit"
    AUTO_LINK = "auto_link", "Auto EAN link"
    AUTO_PRIMARY_SWITCH = "auto_primary_switch", "Auto primary switch"
    MANUAL_OVERRIDE = "manual_override", "Manual override"
    EMERGENCY_SWITCH = "emergency_switch", "Emergency switch"
    OPERATOR_ACKNOWLEDGE = "operator_acknowledge", "Operator acknowledge"
    LANGUAGE_FALLBACK = "language_fallback", "Language fallback"
    MAPPING_TRANSFORM = "mapping_transform", "Mapping transform"
    # pricemanager cost subscriber audit trail (values fit max_length=32)
    COST_SIGNAL_RECEIVED = "cost_signal_received", "Cost signal received"
    COST_IGNORED_NON_PRIMARY = "cost_ignored_non_primary", "Cost ignored (non-primary)"
    COST_IGNORED_NO_LINK = "cost_ignored_no_link", "Cost ignored (no link)"
    COST_SKIPPED_ADMIN_OVERRIDE = "cost_skipped_admin_override", "Cost skipped (admin override)"
    COST_SKIPPED_RESOLUTION_FAILED = "cost_skipped_resolution_failed", "Cost skipped (resolution failed)"
    # operator force-unlink of an auto-linked SP from its RealProduct.
    MANUAL_UNLINK = "manual_unlink", "Manual unlink from RealProduct"
    # operator merge of two RealProducts sharing an EAN.
    MANUAL_MERGE = "manual_merge", "Manual merge by EAN"
    # multi-source physical race detection.
    PHYSICAL_SKIPPED = "physical_skipped", "Physical skipped (non-primary)"
    PHYSICAL_OVERWRITE = "physical_overwrite", "Physical overwrite (opt-in)"


CHANGE_LOG_SOURCES: frozenset[str] = frozenset(ChangeLogSource.values)


class MappingValueModifier(models.TextChoices):
    """Optional unit/string normalisation applied to a source value before push.

    KISS: single modifier per attribute mapping, no chaining, no custom expressions.
    Numeric modifiers operate on Decimal (precision preserved). String modifiers
    operate on str. Type mismatches return the raw value with a warning event."""

    NONE = "none", "None"
    GRAMS_TO_KG = "grams_to_kg", "Grams -> kg (/1000)"
    KG_TO_GRAMS = "kg_to_grams", "Kg -> grams (x1000)"
    MM_TO_CM = "mm_to_cm", "mm -> cm (/10)"
    CM_TO_MM = "cm_to_mm", "cm -> mm (x10)"
    MM_TO_M = "mm_to_m", "mm -> m (/1000)"
    CURRENCY_MINOR_TO_MAJOR = "currency_minor_to_major", "Currency minor -> major (/100)"
    CURRENCY_MAJOR_TO_MINOR = "currency_major_to_minor", "Currency major -> minor (x100)"
    STRING_TRIM = "string_trim", "String trim"
    STRING_LOWERCASE = "string_lowercase", "String lowercase"
    STRING_UPPERCASE = "string_uppercase", "String uppercase"


MAPPING_VALUE_MODIFIERS: frozenset[str] = frozenset(MappingValueModifier.values)


PUSHED_STATUSES: frozenset[str] = frozenset({ProductStatus.PUSHED.value, ProductStatus.PUSHED_PENDING_IMAGES.value})
