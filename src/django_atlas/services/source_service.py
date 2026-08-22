# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import Any

from django.apps import apps
from django.db import transaction
from django.db.models import Q, QuerySet

from django_atlas.enums import PUSHED_STATUSES, SourceKind, SourceType
from django_atlas.models import Source
from django_atlas.services import event_service

_ALLOWED_KINDS = {SourceKind.PROCUREMENT.value, SourceKind.MONITORING.value, SourceKind.ENRICHMENT.value}
_ALLOWED_TYPES = {SourceType.FEED.value, SourceType.MANUAL.value}

# Whitelist of fields editable via update_source (D-mass-assignment).
_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "kind",
        "source_type",
        "review_mode",
        "is_active",
        "default_language",
        "default_currency",
        "country",
        "sku_prefix",
        "default_feature_set_idx",
        "target_warehouse_code",
        "qty_subtract",
        "qty_minimum",
        "company_name",
        "contact_email",
        "contact_phone",
        "contact_person",
        "notes",
        "lead_time_days",
        "is_trusted",
        "currency",
        # primary-only physical writes opt-in.
        "allow_physical_writes_from_non_primary",
        # auto-primary selection knobs (CMS Overview tab).
        "primary_strategy",
        "primary_switch_cooldown_hours",
        "primary_switch_hysteresis_pct",
        "eval_frequency",
    }
)


def _validate_kind_type(*, kind: str | None, source_type: str | None) -> None:
    if kind is not None and kind not in _ALLOWED_KINDS:
        raise ValueError(f"kind '{kind}' not supported. Allowed: 'procurement', 'monitoring', 'enrichment'.")
    if source_type is not None and source_type not in _ALLOWED_TYPES:
        raise ValueError(f"source_type '{source_type}' not supported in MVP. Allowed: 'feed', 'manual'.")


def list_sources(
    *, kind: str | None = None, type: str | None = None, is_active: bool | None = None, search: str | None = None
) -> QuerySet[Source]:
    qs = Source.objects.all()
    if kind is not None:
        qs = qs.filter(kind=kind)
    if type is not None:
        qs = qs.filter(source_type=type)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    if search:
        qs = qs.filter(Q(idx__icontains=search) | Q(name__icontains=search))
    return qs


def get_source(idx: str) -> Source:
    try:
        return Source.objects.get(idx=idx)
    except Source.DoesNotExist as exc:
        raise ValueError(f"Source '{idx}' not found") from exc


def source_exists(idx: str) -> bool:
    return Source.objects.filter(idx=idx).exists()


def _resolve_fk(model_label: str, model_class: type, pk: int | None):
    if pk is None:
        return None
    try:
        return model_class.objects.get(pk=pk)
    except model_class.DoesNotExist as exc:
        raise ValueError(f"{model_label} with id={pk} not found") from exc


def _resolve_regional_fks(
    *,
    default_language_id: int | None = None,
    default_currency_id: int | None = None,
    country_id: int | None = None,
    currency_id: int | None = None,
) -> dict:
    """C4: views pass `_id` ints, service resolves FK objects internally."""
    from django_regional.models import Country, Currency, Language

    resolved: dict = {}
    if default_language_id is not None:
        resolved["default_language"] = _resolve_fk("Language", Language, default_language_id)
    if default_currency_id is not None:
        resolved["default_currency"] = _resolve_fk("Currency", Currency, default_currency_id)
    if country_id is not None:
        resolved["country"] = _resolve_fk("Country", Country, country_id)
    if currency_id is not None:
        resolved["currency"] = _resolve_fk("Currency", Currency, currency_id)
    return resolved


def list_active_sources() -> list[Source]:
    """Return all sources with is_active=True. Used by bulk-push for cross-source flows."""
    return list(Source.objects.filter(is_active=True))


def resolve_id_by_idx(idx: str) -> int | None:
    """Returns Source PK for the given idx, or None when not found.

    Used by view layer to translate URL-side `idx` slugs into FK queries
    without leaking ORM into the view (C4 layer enforcement).
    """
    return Source.objects.filter(idx=idx).values_list("id", flat=True).first()


def create_source(
    *,
    idx: str,
    name: str,
    default_language=None,
    default_currency=None,
    kind: str = SourceKind.PROCUREMENT.value,
    source_type: str = SourceType.FEED.value,
    review_mode: str = "manual",
    is_active: bool = True,
    is_trusted: bool = True,
    country=None,
    currency=None,
    sku_prefix: str = "",
    default_feature_set_idx: str | None = None,
    target_warehouse_code: str | None = None,
    qty_subtract: int = 0,
    qty_minimum: int = 0,
    default_language_id: int | None = None,
    default_currency_id: int | None = None,
    country_id: int | None = None,
    currency_id: int | None = None,
    **optional: Any,
) -> Source:
    # C4: accept either resolved FK objects (legacy callers) or `_id` ints.
    if default_language is None or default_currency is None or country is None or currency is None:
        resolved = _resolve_regional_fks(
            default_language_id=default_language_id if default_language is None else None,
            default_currency_id=default_currency_id if default_currency is None else None,
            country_id=country_id if country is None else None,
            currency_id=currency_id if currency is None else None,
        )
        default_language = default_language or resolved.get("default_language")
        default_currency = default_currency or resolved.get("default_currency")
        country = country or resolved.get("country")
        currency = currency or resolved.get("currency")
    if default_language is None:
        raise ValueError("default_language is required (pass default_language or default_language_id)")
    if default_currency is None:
        raise ValueError("default_currency is required (pass default_currency or default_currency_id)")
    _validate_kind_type(kind=kind, source_type=source_type)
    if Source.objects.filter(idx=idx).exists():
        raise ValueError(f"Source with idx '{idx}' already exists.")
    return Source.objects.create(
        idx=idx,
        name=name,
        kind=kind,
        source_type=source_type,
        review_mode=review_mode,
        is_active=is_active,
        is_trusted=is_trusted,
        default_language=default_language,
        default_currency=default_currency,
        country=country,
        currency=currency,
        sku_prefix=sku_prefix,
        default_feature_set_idx=default_feature_set_idx,
        target_warehouse_code=target_warehouse_code,
        qty_subtract=qty_subtract,
        qty_minimum=qty_minimum,
        **optional,
    )


def update_source(idx: str, **fields: Any) -> Source:
    # C4: accept `_id` ints from API layer, resolve FK objects internally.
    fk_kwargs: dict = {}
    for fk_id_key in ("default_language_id", "default_currency_id", "country_id", "currency_id"):
        if fk_id_key in fields:
            fk_kwargs[fk_id_key] = fields.pop(fk_id_key)
    if fk_kwargs:
        fields.update(_resolve_regional_fks(**{k: fk_kwargs.get(k) for k in fk_kwargs}))
    invalid = set(fields) - _EDITABLE_FIELDS
    if invalid:
        raise ValueError(f"Fields not editable via update_source: {sorted(invalid)}")
    source = get_source(idx)
    _validate_kind_type(kind=fields.get("kind"), source_type=fields.get("source_type"))
    for field, value in fields.items():
        setattr(source, field, value)
    source.save()
    return source


def set_credentials(idx: str, credentials: dict) -> Source:
    """C1: dedicated write path for the sensitive `credentials` field.

    Deliberately bypasses `_EDITABLE_FIELDS` / `update_source` — credentials writes are
    routed through `SourceCredentialsView` only (super-user, audited), mirroring the
    equally sensitive read path.
    """
    source = get_source(idx)
    source.credentials = credentials
    source.save(update_fields=["credentials", "modified_at"])
    return source


def _count_affected_links(source: Source) -> int:
    try:
        link_model = apps.get_model("django_atlas", "SourceProductLink")
    except LookupError:
        return 0
    return link_model.objects.filter(source=source).count()


def _list_affected_pushed_skus(source: Source, *, cap: int = 100) -> list[str]:
    try:
        sp_model = apps.get_model("django_atlas", "SourceProduct")
    except LookupError:
        return []
    qs = sp_model.objects.filter(source=source, status__in=PUSHED_STATUSES).select_related("real_product")
    skus = list(qs.values_list("real_product__sku", flat=True)[:cap])
    return [sku for sku in skus if sku]


def delete_source(idx: str, *, force: bool = False) -> dict:
    if not force:
        update_source(idx, is_active=False)
        return {"mode": "soft", "source_idx": idx}

    with transaction.atomic():
        source = get_source(idx)
        if hasattr(source, "feeds"):
            source.feeds.update(is_active=False)

        affected_links_count = _count_affected_links(source)
        affected_pushed_skus = _list_affected_pushed_skus(source)

        event_service.record(
            event_type="source_deleted",
            severity="warning",
            source=None,
            message=(
                f"Source '{idx}' hard-deleted. "
                f"{affected_links_count} links removed, "
                f"{len(affected_pushed_skus)} pushed SKUs orphaned in PIM (manual cleanup required)."
            ),
            details={
                "source_idx": idx,
                "affected_links_count": affected_links_count,
                "affected_pushed_skus": affected_pushed_skus,
            },
        )

        source.delete()

    return {
        "mode": "hard",
        "source_idx": idx,
        "affected_links_count": affected_links_count,
        "affected_pushed_skus_count": len(affected_pushed_skus),
    }
