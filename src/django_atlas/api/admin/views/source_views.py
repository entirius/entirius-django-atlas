# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for Source resource (CRUD + soft/hard delete + delete-impact).

C4: ZERO Django models imported here — all ORM via source_service / event_service.
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
from django_atlas.api.admin.permissions import IsAdminUser, IsSuperUser
from django_atlas.api.admin.views._helpers import raise_as_drf
from django_atlas.enums import EventSeverity, EventType
from django_atlas.schemas.requests.source import (
    SourceCreateRequest,
    SourceCredentialsUpdateRequest,
    SourceUpdateRequest,
)
from django_atlas.schemas.responses.data_keys import DataKeysResponse
from django_atlas.schemas.responses.data_values import DataValuesResponse
from django_atlas.schemas.responses.source import (
    SourceCredentialsResponse,
    SourceDeleteImpactResponse,
    SourceDeleteResponse,
    SourceListResponse,
    SourceResponse,
)
from django_atlas.services import data_inspector_service, event_service, source_service


def _serialize(s) -> dict:
    return SourceResponse.model_validate(s).model_dump(mode="json")


_TAGS = ["Sources"]


class SourceViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None
    lookup_field = "idx"

    @extend_schema(
        tags=_TAGS,
        summary="List sources",
        description="Returns paginated list of sources, optionally filtered.",
        parameters=[
            OpenApiParameter("kind", str, OpenApiParameter.QUERY, required=False, description="Filter by kind"),
            OpenApiParameter("type", str, OpenApiParameter.QUERY, required=False, description="Filter by source_type"),
            OpenApiParameter(
                "is_active", bool, OpenApiParameter.QUERY, required=False, description="Active flag filter"
            ),
            OpenApiParameter(
                "search", str, OpenApiParameter.QUERY, required=False, description="Search idx/name (icontains)"
            ),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: SourceListResponse},
    )
    def list(self, request: Request) -> Response:
        is_active = request.query_params.get("is_active")
        is_active_bool = None
        if is_active is not None:
            is_active_bool = is_active.lower() in ("true", "1", "yes")

        qs = source_service.list_sources(
            kind=request.query_params.get("kind"),
            type=request.query_params.get("type"),
            is_active=is_active_bool,
            search=request.query_params.get("search"),
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

    @extend_schema(
        tags=_TAGS,
        summary="Retrieve source",
        description="Get a source by idx.",
        responses={200: SourceResponse, 404: None},
    )
    def retrieve(self, request: Request, idx: str) -> Response:
        try:
            source = source_service.get_source(idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Source '{idx}' not found") from exc
        return Response(_serialize(source))

    @extend_schema(
        tags=_TAGS,
        summary="Create source",
        description="Create a new source.",
        request=SourceCreateRequest,
        responses={201: SourceResponse, 400: None},
    )
    def create(self, request: Request) -> Response:
        try:
            data = SourceCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            source = source_service.create_source(
                idx=data.idx,
                name=data.name,
                default_language_id=data.default_language_id,
                default_currency_id=data.default_currency_id,
                country_id=data.country_id,
                currency_id=data.currency_id,
                kind=data.kind,
                source_type=data.source_type,
                review_mode=data.review_mode,
                is_active=data.is_active,
                is_trusted=data.is_trusted,
                sku_prefix=data.sku_prefix,
                default_feature_set_idx=data.default_feature_set_idx,
                target_warehouse_code=data.target_warehouse_code,
                qty_subtract=data.qty_subtract,
                qty_minimum=data.qty_minimum,
                company_name=data.company_name,
                contact_email=data.contact_email,
                contact_phone=data.contact_phone,
                contact_person=data.contact_person,
                notes=data.notes,
                lead_time_days=data.lead_time_days,
            )
        except ValueError as exc:
            raise drf_exceptions.ValidationError(str(exc)) from exc
        return Response(_serialize(source), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update source",
        description="Patch an existing source (partial fields).",
        request=SourceUpdateRequest,
        responses={200: SourceResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, idx: str) -> Response:
        try:
            data = SourceUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = data.model_dump(exclude_unset=True)
        try:
            source = source_service.update_source(idx, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(source))

    @extend_schema(
        tags=_TAGS,
        summary="Delete source (soft default, hard with ?force=true)",
        description="Decision #37: default soft delete sets is_active=False; ?force=true performs hard cascade.",
        parameters=[
            OpenApiParameter(
                "force", bool, OpenApiParameter.QUERY, required=False, description="Hard delete cascade if true"
            )
        ],
        responses={200: SourceDeleteResponse, 404: None},
    )
    def destroy(self, request: Request, idx: str) -> Response:
        force_param = request.query_params.get("force", "false").lower()
        force = force_param in ("true", "1", "yes")
        try:
            result = source_service.delete_source(idx, force=force)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Source '{idx}' not found") from exc
        return Response(result)

    @extend_schema(
        tags=_TAGS,
        summary="List SourceProduct.data keys + reserved tokens",
        description=(
            "Returns the 3 reserved tokens (__name__, __cost__, __ean__) plus every dot-path "
            "observed in SourceProduct.data across a sample (default 100 rows), ranked by "
            "presence_pct DESC. Feeds the CMS mapping form combobox so operators stop typing "
            "JSON keys from memory. Cached 5 min per source — invalidated by execute_feed_task."
        ),
        parameters=[
            OpenApiParameter(
                "sample_size",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="How many SourceProduct rows to sample (default 100, min 1)",
            )
        ],
        responses={200: DataKeysResponse, 404: None},
    )
    def data_keys(self, request: Request, idx: str) -> Response:
        try:
            sample_size = int(request.query_params.get("sample_size", 100))
        except (TypeError, ValueError) as exc:
            raise drf_exceptions.ValidationError("sample_size must be an integer") from exc
        try:
            payload = data_inspector_service.list_keys(idx, sample_size=sample_size)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(payload)

    @extend_schema(
        tags=_TAGS,
        summary="List distinct SourceProduct.data values for a source_field",
        description=(
            "Returns distinct values + counts for the given source_field across the source's "
            "SourceProducts. Powers the source_value picker in CategoryMapping rows so the "
            "operator sees real source vocabulary (e.g. 'Pozostałe (87)') instead of typing "
            "from memory. Dot-paths supported for nested JSON (e.g. info.options.value). "
            "Cached 5 min per (source, source_field) and invalidated together with data-keys "
            "on `execute_feed_task` success."
        ),
        parameters=[
            OpenApiParameter(
                "source_field",
                str,
                OpenApiParameter.QUERY,
                required=True,
                description="Dot-path into SourceProduct.data (e.g. category_path, info.options.value)",
            ),
            OpenApiParameter(
                "limit",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="Max distinct values returned (default 500, min 1, max 5000)",
            ),
        ],
        responses={200: DataValuesResponse, 400: None, 404: None},
    )
    def data_values(self, request: Request, idx: str) -> Response:
        source_field = request.query_params.get("source_field", "").strip()
        if not source_field:
            raise drf_exceptions.ValidationError("source_field query param is required")
        try:
            limit = int(request.query_params.get("limit", 500))
        except (TypeError, ValueError) as exc:
            raise drf_exceptions.ValidationError("limit must be an integer") from exc
        if limit < 1 or limit > 5000:
            raise drf_exceptions.ValidationError("limit must be between 1 and 5000")
        try:
            payload = data_inspector_service.list_values(idx, source_field, limit=limit)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(payload)

    @extend_schema(
        tags=_TAGS,
        summary="Delete impact preview",
        description="Returns counts of affected SourceProductLink + pushed SKUs without deleting anything.",
        responses={200: SourceDeleteImpactResponse, 404: None},
    )
    def delete_impact(self, request: Request, idx: str) -> Response:
        try:
            source = source_service.get_source(idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Source '{idx}' not found") from exc
        affected_links_count = source_service._count_affected_links(source)  # noqa: SLF001 — internal but stable
        pushed_skus = source_service._list_affected_pushed_skus(source)  # noqa: SLF001
        return Response(
            {
                "affected_links_count": affected_links_count,
                "affected_pushed_skus_count": len(pushed_skus),
                "affected_pushed_skus_sample": pushed_skus[:10],
            }
        )


class SourceCredentialsView(viewsets.ViewSet):
    """Sensitive: read-only access to Source.credentials, super-user only, audited (C1)."""

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsSuperUser]
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="Read source credentials (super-user only)",
        description="Returns the connector credentials JSON. Each access emits an audit event.",
        responses={200: SourceCredentialsResponse, 403: None, 404: None},
    )
    def retrieve(self, request: Request, idx: str) -> Response:
        try:
            source = source_service.get_source(idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Source '{idx}' not found") from exc
        event_service.record(
            event_type=EventType.SOURCE_CREDENTIALS_VIEWED.value,
            severity=EventSeverity.INFO.value,
            source=source,
            message=f"User {getattr(request.user, 'id', '?')} viewed credentials for source '{idx}'",
            details={"viewed_by_user_id": getattr(request.user, "id", None)},
        )
        return Response(
            SourceCredentialsResponse.model_validate({"idx": source.idx, "credentials": source.credentials}).model_dump(
                mode="json"
            )
        )

    @extend_schema(
        tags=_TAGS,
        summary="Write source credentials (super-user only)",
        description="Replaces the connector credentials JSON. Each write emits an audit event.",
        request=SourceCredentialsUpdateRequest,
        responses={200: SourceCredentialsResponse, 403: None, 404: None},
    )
    def partial_update(self, request: Request, idx: str) -> Response:
        try:
            data = SourceCredentialsUpdateRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            source = source_service.set_credentials(idx, data.credentials)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or f"Source '{idx}' not found") from exc
        event_service.record(
            event_type=EventType.SOURCE_CREDENTIALS_UPDATED.value,
            severity=EventSeverity.INFO.value,
            source=source,
            message=f"User {getattr(request.user, 'id', '?')} updated credentials for source '{idx}'",
            details={"updated_by_user_id": getattr(request.user, "id", None)},
        )
        return Response(
            SourceCredentialsResponse.model_validate({"idx": source.idx, "credentials": source.credentials}).model_dump(
                mode="json"
            )
        )
