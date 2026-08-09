# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API view for bulk push (cross-source or single source).

C4: ZERO Django models imported here — all ORM via source_service / push_service.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import extend_schema
from pydantic import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.api.admin.views._helpers import raise_as_drf
from django_atlas.schemas.requests.push import BulkPushRequest
from django_atlas.schemas.responses.product import BulkPushResponse
from django_atlas.services import push_service, source_service

_TAGS = ["Source Push"]


class BulkPushView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=_TAGS,
        summary="Push approved source products to PIM",
        description="Push every approved SP for one source (source_idx given) or all active sources.",
        request=BulkPushRequest,
        responses={200: BulkPushResponse, 400: None, 404: None},
    )
    def post(self, request: Request) -> Response:
        try:
            data = BulkPushRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)

        if data.source_idx:
            try:
                sources = [source_service.get_source(data.source_idx)]
            except ValueError as exc:
                raise_as_drf(exc)
        else:
            sources = source_service.list_active_sources()

        total_success = 0
        total_failed = 0
        preflight_failed: list[str] = []
        for source in sources:
            result = push_service.push_approved_for_source(source.id, request.user)
            total_success += result.get("success", 0)
            total_failed += result.get("failed", 0)
            if result.get("preflight_failed"):
                preflight_failed.append(source.idx)
        return Response(
            {
                "sources_processed": len(sources),
                "success": total_success,
                "failed": total_failed,
                "preflight_failed": preflight_failed,
            }
        )
