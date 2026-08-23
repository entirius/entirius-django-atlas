# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Atlas provider for the django-lookup module (kind `atlas_source_product`).

Lookup never imports atlas: it loads this module lazily by dotted path from
`settings.LOOKUP_PROVIDERS = {"atlas_source_product": "django_atlas.services.lookup_provider"}` and
calls the module-level functions below (duck-typed against `django_lookup.providers.base`). Nothing
from the consuming module is imported here — `ProviderItem` / `BasicData` are mirrored, their field
names being the contract (`django_lookup.providers.base` is authoritative).

Items are the *candidate pool*: source products still waiting for a RealProduct — `real_product IS
NULL` and not rejected. Linking one drops it from the pool, which is how its fingerprint row gets
deleted (the lookup refresh task deletes a row whose item the provider no longer serves).

`ref` = `<source.idx>:<external_id>` (stable across re-imports, unlike the pk). `detail_url` still
needs the pk — the admin API exposes source products cross-source, by pk only.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from django_atlas.enums import ProductStatus
from django_atlas.models import AttributeMappingTargetType, SourceAttributeMapping, SourceProduct
from django_atlas.services.value_transformer import transform

REF_SEPARATOR = ":"
DETAIL_URL = "/api/atlas/v2/admin/products/{pk}/"
# data keys read when no SourceAttributeMapping points at the target: first hit wins.
FALLBACK_KEYS = {
    "brand": ("brand", "manufacturer"),
    "mpn": ("mpn", "manufacturer_part_number"),
    "weight": ("weight",),
    "width": ("width",),
    "height": ("height",),
    "deep": ("deep", "depth"),
}
_PHYSICAL_ATTRS = ("weight", "width", "height", "deep")
_RELATED = ("source__default_language",)


@dataclass(frozen=True)
class ProviderItem:
    """Mirror of django_lookup.providers.base.ProviderItem (duplicated to avoid the import)."""

    ref: str
    gtin: str | None = None
    brand: str | None = None
    mpn: str | None = None
    name_by_lang: dict[str, str] = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    image_path_or_url: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BasicData:
    """Mirror of django_lookup.providers.base.BasicData."""

    ref: str
    name: str
    brand: str = ""
    gtin: str = ""
    mpn: str = ""
    image_url: str = ""


def candidates():
    """Source products still looking for a RealProduct — the only rows this provider fingerprints."""
    return SourceProduct.objects.filter(real_product__isnull=True).exclude(status=ProductStatus.REJECTED)


def iter_items(since: datetime | None = None) -> Iterator[ProviderItem]:
    queryset = candidates()
    if since is not None:
        queryset = queryset.filter(modified_at__gte=since)
    mappings: dict[int, dict[str, SourceAttributeMapping]] = {}
    for source_product in queryset.select_related(*_RELATED).order_by("id").iterator(chunk_size=500):
        yield _item(source_product, _mappings_for(source_product.source_id, mappings))


def get_item(ref: str) -> ProviderItem:
    source_product = _require(ref)
    return _item(source_product, _mappings_for(source_product.source_id, {}))


def basic(ref: str) -> BasicData:
    item = get_item(ref)
    return BasicData(
        ref=item.ref,
        name=next(iter(item.name_by_lang.values()), ""),
        brand=item.brand or "",
        gtin=item.gtin or "",
        mpn=item.mpn or "",
        image_url=item.image_path_or_url or "",
    )


def detail_url(ref: str) -> str:
    return DETAIL_URL.format(pk=_require(ref).pk)


def signal_specs() -> list[dict]:
    """Senders django-lookup connects so a fingerprint follows the catalog (see its signals.py).

    Every save is enqueued: the task rebuilds the row from the current data (a changed `data_hash`)
    or deletes it (`real_product` set, status rejected). Deciding here would need the pre-save row.
    """
    return [{"model": "django_atlas.SourceProduct", "signal": "post_save", "ref": ref_for}]


def ref_for(source_product: SourceProduct) -> str:
    return f"{source_product.source.idx}{REF_SEPARATOR}{source_product.external_id}"


def _require(ref: str) -> SourceProduct:
    source_idx, separator, external_id = ref.partition(REF_SEPARATOR)
    source_product = (
        candidates().filter(source__idx=source_idx, external_id=external_id).select_related(*_RELATED).first()
        if separator
        else None
    )
    if source_product is None:
        raise LookupError(f"no unlinked SourceProduct for ref={ref!r}")
    return source_product


def _item(source_product: SourceProduct, mappings: dict[str, SourceAttributeMapping]) -> ProviderItem:
    data = source_product.data or {}
    language = source_product.source.default_language.iso2.lower()
    return ProviderItem(
        ref=ref_for(source_product),
        gtin=source_product.ean,
        brand=_text(_value(data, mappings, "brand")),
        mpn=_text(_value(data, mappings, "mpn")),
        name_by_lang={language: source_product.name} if source_product.name else {},
        attrs={name: _value(data, mappings, name) for name in _PHYSICAL_ATTRS},
        image_path_or_url=next(iter(source_product.image_urls or []), None),
        updated_at=source_product.modified_at,
    )


def _text(value) -> str | None:
    """Brand/MPN are text to the normalisers; a feed may still deliver them as a number."""
    return str(value).strip() or None if value not in (None, "") else None


def _mappings_for(source_id: int, cache: dict[int, dict[str, SourceAttributeMapping]]) -> dict:
    """target_identifier -> mapping, for the active profiles of one source. Cached per iteration."""
    if source_id not in cache:
        rows = SourceAttributeMapping.objects.filter(
            profile__source_id=source_id, profile__is_active=True, target_identifier__in=list(FALLBACK_KEYS)
        ).exclude(target_type=AttributeMappingTargetType.SKIP)
        cache[source_id] = {row.target_identifier: row for row in rows}
    return cache[source_id]


def _value(data: dict, mappings: dict[str, SourceAttributeMapping], target: str):
    """Mapped source field first (with its modifier applied), else the conventional raw keys."""
    if (mapping := mappings.get(target)) and (raw := data.get(mapping.source_field)) not in (None, ""):
        return transform(raw, mapping.modifier).value
    return next((data[key] for key in FALLBACK_KEYS[target] if data.get(key) not in (None, "")), None)
