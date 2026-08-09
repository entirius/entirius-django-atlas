# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Shared helpers for admin API views.

Single responsibility: convert service-layer exceptions (ValueError, InvalidOperation)
into DRF exceptions so the v2 exception handler can emit the standard v2 error shape
(`{error, message, debug_id, details}`).

NEVER use `return Response({"detail": str(e)}, ...)` in views — that bypasses the
v2 exception handler and leaks legacy `{detail}` shape. Use `raise_as_drf(exc)` or
raise the appropriate `drf_exceptions.*` directly.
"""

from decimal import InvalidOperation
from typing import NoReturn

from rest_framework import exceptions as drf_exceptions


def raise_as_drf(exc: ValueError | InvalidOperation) -> NoReturn:
    """Raise the appropriate DRF exception so the v2 handler emits structured shape.

    Convention across `django_atlas.services`: ``raise ValueError(f"<Resource> ... not found")``
    means resource lookup miss → HTTP 404. Anything else is a domain validation failure → 400.
    """
    msg = str(exc)
    lowered = msg.lower()
    if lowered.endswith("not found") or " not found " in lowered or lowered.endswith("not found."):
        raise drf_exceptions.NotFound(msg) from exc
    raise drf_exceptions.ValidationError(msg) from exc
