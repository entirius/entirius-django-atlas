# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SKU validators — atlas owns its own copy (do NOT import from django_pim).

Leaf module — imports no Django models, so any layer may import it without
violating the dependency direction (API → Services → Models).

Ported from `django_pim.validators`: RealProduct.sku is created once in
PIM and consumed by every module that routes admin endpoints by SKU (django-pim, -matrix,
-pricemanager, -qms, -atlas). A `<path:sku>` URL converter matches slashes, so a SKU ending
in a reserved sub-route word would be shadowed by — or shadow — that sub-route. Atlas is
one of the SKU consumers (`pim-sku/<path:sku>/...` bridge endpoints) AND one of the few
non-PIM writers of `RealProduct.sku` (`pim_writer.generate_sku` on INIT push), so it needs
its own copy of the guard rather than depending on django_pim at runtime.
"""

# Literal segments used by SKU-addressed API sub-routes across every module that routes by
# SKU in the URL path. Kept in sync with `django_pim.validators.RESERVED_SKU_SUFFIXES` —
# update both when either module's routing changes.
RESERVED_SKU_SUFFIXES = frozenset(
    {
        # django-pim product sub-routes
        "copy-attributes",
        "copy-translations",
        "add-to-channel",
        "toggle-override",
        "toggle-media-override",
        "links",
        "pictures",
        "files",
        "videos",
        # django-qms
        "edit",
        # django-pricemanager
        "flush-special",
        "preview",
        "history",
        # django-atlas (was django-suppliers)
        "changes",
        "acknowledge",
        "force-repush",
        "set-primary-source",
        "reset-primary-to-auto",
    }
)

# Whole-SKU literals that collide with a SIBLING route (not a suffix) — `products/bulk/`
# is the bulk-update endpoint, so a product whose entire SKU is "bulk" would be shadowed.
RESERVED_SKU_WHOLE = frozenset({"bulk"})


def validate_routable_sku(sku: str) -> None:
    """Reject SKUs whose slash-suffix collides with an API sub-route.

    A `<path:sku>` URL converter cannot tell `.../products/X/pictures/` (the pictures
    sub-route of product "X") from the detail of a product literally named
    "X/pictures". The middle of a SKU is unambiguous (`AB/pictures/CD` resolves fine);
    only a reserved trailing segment — optionally followed by an integer pk, as in
    `.../links/<pk>/` — breaks. Raises ValueError so callers surface a 400.
    """
    if sku in RESERVED_SKU_WHOLE:
        raise ValueError(f"SKU '{sku}' collides with the reserved '{sku}/' route. Choose another SKU.")
    if "/" not in sku:
        return
    segments = sku.split("/")
    last = segments[-1]
    reserved = None
    if last in RESERVED_SKU_SUFFIXES:
        reserved = last
    elif last.isdigit() and len(segments) >= 2 and segments[-2] in RESERVED_SKU_SUFFIXES:
        reserved = segments[-2]
    if reserved is not None:
        raise ValueError(
            f"SKU '{sku}' may not end with the reserved route word '{reserved}' "
            f"— it collides with an API sub-route. Change the SKU suffix."
        )
