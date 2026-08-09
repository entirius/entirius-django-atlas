# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView


class IsAdminUser(IsAuthenticated):
    """Authenticated + (is_staff OR is_superuser).

    Pattern intentionally diverges from `rest_framework.permissions.IsAdminUser`
    (which only checks `is_staff`) — Volkanos admin endpoints accept both flags.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not super().has_permission(request, view):
            return False
        return bool(request.user and (request.user.is_staff or request.user.is_superuser))


class IsSuperUser(IsAuthenticated):
    """Authenticated + is_superuser. Used for sensitive endpoints (credentials, audit-only)."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not super().has_permission(request, view):
            return False
        return bool(request.user and request.user.is_superuser)
