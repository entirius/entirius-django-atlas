# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for ImportLog (read-only) + IntegrationEvent (read + acknowledge).

C4: ZERO Django models imported here — all ORM via services.
"""

from datetime import datetime

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions as drf_exceptions
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.pagination import AdminPageNumberPagination
from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.schemas.responses.event import IntegrationEventListResponse, IntegrationEventResponse
from django_atlas.schemas.responses.log import ImportLogListResponse, ImportLogResponse
from django_atlas.services import event_service, log_service, source_service


def _serialize_log(log) -> dict:
    payload = ImportLogResponse.model_validate(log).model_dump(mode="json")
    payload["run_id"] = str(log.run_id)
    return payload


def _serialize_event(ev) -> dict:
    return IntegrationEventResponse.model_validate(ev).model_dump(mode="json")


_TAGS_LOG = ["Source Import Logs"]
_TAGS_EVENT = ["Source Events"]


class ImportLogViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS_LOG,
        summary="List import logs",
        parameters=[
            OpenApiParameter("feed", int, OpenApiParameter.QUERY, required=False, description="Filter by feed PK"),
            OpenApiParameter("status", str, OpenApiParameter.QUERY, required=False, description="Status filter"),
            OpenApiParameter("mode", str, OpenApiParameter.QUERY, required=False, description="Mode filter"),
            OpenApiParameter(
                "started_at_after", str, OpenApiParameter.QUERY, required=False, description="ISO 8601 date"
            ),
            OpenApiParameter(
                "started_at_before", str, OpenApiParameter.QUERY, required=False, description="ISO 8601 date"
            ),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: ImportLogListResponse, 400: None},
    )
    def list(self, request: Request) -> Response:
        feed_id: int | None = None
        if feed := request.query_params.get("feed"):
            try:
                feed_id = int(feed)
            except ValueError as exc:
                raise drf_exceptions.ValidationError("feed must be int") from exc
        started_at_after: datetime | None = None
        started_at_before: datetime | None = None
        for param, target in (("started_at_after", "after"), ("started_at_before", "before")):
            if value := request.query_params.get(param):
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError as exc:
                    raise drf_exceptions.ValidationError(f"{param} must be ISO 8601") from exc
                if target == "after":
                    started_at_after = parsed
                else:
                    started_at_before = parsed
        qs = log_service.list_logs(
            feed_id=feed_id,
            status=request.query_params.get("status"),
            mode=request.query_params.get("mode"),
            started_at_after=started_at_after,
            started_at_before=started_at_before,
        )
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": [_serialize_log(log) for log in page],
            }
        )

    @extend_schema(tags=_TAGS_LOG, summary="Retrieve import log", responses={200: ImportLogResponse, 404: None})
    def retrieve(self, request: Request, pk: int) -> Response:
        try:
            log = log_service.get_log(pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "ImportLog not found") from exc
        return Response(_serialize_log(log))


class IntegrationEventViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS_EVENT,
        summary="List integration events",
        parameters=[
            OpenApiParameter("event_type", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("severity", str, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("source", str, OpenApiParameter.QUERY, required=False, description="Source.idx"),
            OpenApiParameter(
                "acknowledged", bool, OpenApiParameter.QUERY, required=False, description="True/False filter"
            ),
            OpenApiParameter("search", str, OpenApiParameter.QUERY, required=False, description="Search message"),
            OpenApiParameter("page", int, OpenApiParameter.QUERY, required=False),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, required=False),
        ],
        responses={200: IntegrationEventListResponse},
    )
    def list(self, request: Request) -> Response:
        source_id: int | None = None
        if source_idx := request.query_params.get("source"):
            source_id = source_service.resolve_id_by_idx(source_idx)
            if source_id is None:
                return Response({"count": 0, "next": None, "previous": None, "results": []})
        ack_param = request.query_params.get("acknowledged")
        ack_bool: bool | None = None
        if ack_param is not None:
            ack_bool = ack_param.lower() in ("true", "1", "yes")
        qs = event_service.list_events(
            severity=request.query_params.get("severity"),
            event_type=request.query_params.get("event_type"),
            source_id=source_id,
            acknowledged=ack_bool,
            search=request.query_params.get("search"),
        ).order_by("-created_at")
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": [_serialize_event(ev) for ev in page],
            }
        )

    @extend_schema(
        tags=_TAGS_EVENT, summary="Retrieve integration event", responses={200: IntegrationEventResponse, 404: None}
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        try:
            ev = event_service.get_event(pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Event not found") from exc
        return Response(_serialize_event(ev))

    @extend_schema(
        tags=_TAGS_EVENT, summary="Acknowledge event (idempotent)", responses={200: IntegrationEventResponse, 404: None}
    )
    def acknowledge(self, request: Request, pk: int) -> Response:
        try:
            ev = event_service.acknowledge(pk, request.user)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Event not found") from exc
        return Response(_serialize_event(ev))
