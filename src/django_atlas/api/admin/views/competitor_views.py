# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Competitor facade — monitoring-kind projection of Source.

A human sees "Competitor", never "monitoring source". Thin wrapper around
`source_service` — `kind` is forced to monitoring server-side and never accepted from
the client. Nested resources stay under `/sources/{idx}/...` (YAGNI, plan-notes §2.10).
UI-wise Competitor is API-only for now — the CMS tab returns with the first real
monitoring connector (architecture-notes §5 Element 1b).

C4: ZERO Django models imported here — all ORM via source_service.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.pagination import AdminPageNumberPagination
from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.api.admin.views._helpers import raise_as_drf
from django_atlas.enums import SourceKind
from django_atlas.schemas.requests.competitor import CompetitorCreateRequest, CompetitorUpdateRequest
from django_atlas.schemas.responses.competitor import CompetitorListResponse, CompetitorResponse
from django_atlas.services import source_service

_TAGS = ["Competitors"]
_KIND = SourceKind.MONITORING.value


def _serialize(s) -> dict:
    return CompetitorResponse.model_validate(s).model_dump(mode="json")


class CompetitorViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None
    lookup_field = "idx"

    @extend_schema(
        tags=_TAGS,
        summary="List competitors",
        description="Sources with kind=monitoring, projected as Competitor.",
        parameters=[
            OpenApiParameter(
                "is_active", bool, OpenApiParameter.QUERY, required=False, description="Active flag filter"
            ),
            OpenApiParameter(
                "search", str, OpenApiParameter.QUERY, required=False, description="Search idx/name (icontains)"
            ),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: CompetitorListResponse},
    )
    def list(self, request: Request) -> Response:
        is_active = request.query_params.get("is_active")
        is_active_bool = is_active.lower() in ("true", "1", "yes") if is_active is not None else None
        qs = source_service.list_sources(
            kind=_KIND, is_active=is_active_bool, search=request.query_params.get("search")
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [_serialize(s) for s in page]
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": results,
            }
        )

    @extend_schema(tags=_TAGS, summary="Retrieve competitor", responses={200: CompetitorResponse, 404: None})
    def retrieve(self, request: Request, idx: str) -> Response:
        source = self._get_monitoring_source(idx)
        return Response(_serialize(source))

    @extend_schema(
        tags=_TAGS,
        summary="Create competitor",
        request=CompetitorCreateRequest,
        responses={201: CompetitorResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = CompetitorCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            source = source_service.create_source(kind=_KIND, **data.model_dump())
        except ValueError as exc:
            raise drf_exceptions.ValidationError(str(exc)) from exc
        return Response(_serialize(source), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update competitor",
        request=CompetitorUpdateRequest,
        responses={200: CompetitorResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, idx: str) -> Response:
        self._get_monitoring_source(idx)
        try:
            data = CompetitorUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = data.model_dump(exclude_unset=True)
        try:
            source = source_service.update_source(idx, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(source))

    @extend_schema(tags=_TAGS, summary="Soft-delete competitor", responses={200: None, 404: None})
    def destroy(self, request: Request, idx: str) -> Response:
        self._get_monitoring_source(idx)
        result = source_service.delete_source(idx, force=False)
        return Response(result)

    def _get_monitoring_source(self, idx: str):
        try:
            source = source_service.get_source(idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Competitor '{idx}' not found") from exc
        if source.kind != _KIND:
            raise drf_exceptions.NotFound(f"Competitor '{idx}' not found")
        return source
