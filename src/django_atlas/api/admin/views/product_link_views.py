# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for SourceProductLink (cross-source list + set-primary).

C4: ZERO Django models imported here — all ORM via product_link_service / source_service.
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
from django_atlas.schemas.requests.product_link import SourceProductLinkCreateRequest, SourceProductLinkUpdateRequest
from django_atlas.schemas.responses.product_link import SourceProductLinkListResponse, SourceProductLinkResponse
from django_atlas.services import product_link_service, source_service


def _serialize(link) -> dict:
    return SourceProductLinkResponse.model_validate(link).model_dump(mode="json")


_TAGS = ["Product Source Links"]


class SourceProductLinkViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List SourceProductLink (cross-source)",
        parameters=[
            OpenApiParameter("real_product_sku", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("source", str, OpenApiParameter.QUERY, required=False, description="Source.idx"),
            OpenApiParameter("is_active", bool, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: SourceProductLinkListResponse},
    )
    def list(self, request: Request) -> Response:
        source_idx = request.query_params.get("source")
        source_id: int | None = None
        if source_idx:
            source_id = source_service.resolve_id_by_idx(source_idx)
            if source_id is None:
                return Response({"count": 0, "next": None, "previous": None, "results": []})
        is_active = request.query_params.get("is_active")
        is_active_bool = None
        if is_active is not None:
            is_active_bool = is_active.lower() in ("true", "1", "yes")
        qs = product_link_service.list_links(
            real_product_sku=request.query_params.get("real_product_sku"), source_id=source_id, is_active=is_active_bool
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": [_serialize(link) for link in page],
            }
        )

    @extend_schema(
        tags=_TAGS, summary="Retrieve product source link", responses={200: SourceProductLinkResponse, 404: None}
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        try:
            link = product_link_service.get_link(pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Link not found") from exc
        return Response(_serialize(link))

    @extend_schema(
        tags=_TAGS,
        summary="Create product source link",
        request=SourceProductLinkCreateRequest,
        responses={201: SourceProductLinkResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = SourceProductLinkCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            link = product_link_service.create_link(
                real_product_sku=data.real_product_sku,
                source_idx=data.source_idx,
                external_id=data.external_id,
                priority=data.priority,
                is_primary=data.is_primary,
                notes=data.notes,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        if data.is_active is False:
            link = product_link_service.update_link(link.pk, is_active=False)
        return Response(_serialize(link), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update product source link",
        request=SourceProductLinkUpdateRequest,
        responses={200: SourceProductLinkResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, pk: int) -> Response:
        try:
            data = SourceProductLinkUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        try:
            link = product_link_service.update_link(pk, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(link))

    @extend_schema(tags=_TAGS, summary="Delete product source link", responses={204: None})
    def destroy(self, request: Request, pk: int) -> Response:
        product_link_service.delete_link(pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=_TAGS,
        summary="Set this link as the primary source for its SKU (unsets siblings)",
        responses={200: SourceProductLinkResponse, 404: None},
    )
    def set_primary(self, request: Request, pk: int) -> Response:
        try:
            link = product_link_service.set_primary(pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Link not found") from exc
        return Response(_serialize(link))
