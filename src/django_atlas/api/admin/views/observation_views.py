# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API view for the append-only Observation log.

Read-only — Observation has no update/delete path anywhere in this module (see
`services/observation_service.py`). C4: zero Django models imported here.
"""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.pagination import AdminPageNumberPagination
from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.schemas.responses.observation import ObservationListResponse, ObservationResponse
from django_atlas.services import observation_service

_TAGS = ["Observations"]


def _serialize(o) -> dict:
    return ObservationResponse(source_idx=o.source.idx, sku=o.sku, kind=o.kind, value=o.value, ts=o.ts).model_dump(
        mode="json"
    )


def _bool_param(request: Request, name: str, default: bool) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    return raw.lower() in ("true", "1", "yes")


class ObservationViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List observations",
        description=(
            "Append-only external-data observation log (monitoring/enrichment). "
            "latest_per_source=true (default false here) collapses to the newest row "
            "per (sku, kind, source); false returns the full timeline, newest first."
        ),
        parameters=[
            OpenApiParameter("sku", str, OpenApiParameter.QUERY, required=False, description="Filter by PIM sku"),
            OpenApiParameter(
                "kind",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="Filter by kind (monitoring/enrichment)",
            ),
            OpenApiParameter("source", str, OpenApiParameter.QUERY, required=False, description="Filter by Source.idx"),
            OpenApiParameter(
                "latest_per_source",
                bool,
                OpenApiParameter.QUERY,
                required=False,
                description="Collapse to newest row per source (default false)",
            ),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: ObservationListResponse},
    )
    def list(self, request: Request) -> Response:
        qs = observation_service.list_observations_queryset(
            sku=request.query_params.get("sku"),
            kind=request.query_params.get("kind"),
            source_idx=request.query_params.get("source"),
            latest_per_source=_bool_param(request, "latest_per_source", False),
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [_serialize(o) for o in page]
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": results,
            }
        )
