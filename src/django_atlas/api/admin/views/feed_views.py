# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for SourceFeed (nested under source).

C4: ZERO Django models imported here — all ORM via feed_service.
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
from django_atlas.schemas.requests.feed import FeedTriggerRequest, SourceFeedCreateRequest, SourceFeedUpdateRequest
from django_atlas.schemas.responses.feed import (
    FeedTestResponse,
    FeedTriggerResponse,
    SourceFeedListResponse,
    SourceFeedResponse,
)
from django_atlas.services import feed_service


def _serialize(f) -> dict:
    payload = SourceFeedResponse.model_validate(f).model_dump(mode="json")
    if payload.get("last_run_id"):
        payload["last_run_id"] = str(payload["last_run_id"])
    return payload


_TAGS = ["Source Feeds"]


class SourceFeedViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(tags=_TAGS, summary="List source feeds", responses={200: SourceFeedListResponse})
    def list(self, request: Request, source_idx: str) -> Response:
        if not feed_service.source_owns_feeds(source_idx):
            raise drf_exceptions.NotFound(f"Source '{source_idx}' not found")
        qs = feed_service.list_feeds(source_idx)
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [_serialize(f) for f in page]
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": results,
            }
        )

    @extend_schema(tags=_TAGS, summary="Retrieve feed", responses={200: SourceFeedResponse, 404: None})
    def retrieve(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            feed = feed_service.get_feed(source_idx, idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Feed not found") from exc
        return Response(_serialize(feed))

    @extend_schema(
        tags=_TAGS,
        summary="Create feed",
        request=SourceFeedCreateRequest,
        responses={201: SourceFeedResponse, 400: None, 404: None},
    )
    def create(self, request: Request, source_idx: str) -> Response:
        try:
            data = SourceFeedCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        kwargs = data.model_dump(exclude_unset=False)
        try:
            feed = feed_service.create_feed(source_idx, **kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(feed), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update feed",
        request=SourceFeedUpdateRequest,
        responses={200: SourceFeedResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            data = SourceFeedUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        try:
            feed = feed_service.update_feed(source_idx, idx, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize(feed))

    @extend_schema(tags=_TAGS, summary="Delete feed", responses={204: None, 404: None})
    def destroy(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            feed_service.delete_feed(source_idx, idx)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Feed not found") from exc
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=_TAGS,
        summary="Trigger feed run",
        description="Run the feed synchronously (mode=full|delta).",
        request=FeedTriggerRequest,
        responses={200: FeedTriggerResponse, 400: None, 404: None},
    )
    def trigger(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            data = FeedTriggerRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            log = feed_service.trigger_feed_run(source_idx, idx, mode=data.mode, user=request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response({"run_id": str(log.run_id), "status": log.status})

    @extend_schema(
        tags=_TAGS,
        summary="Test feed (sample import)",
        description="Sync connectors return raw products list; async connectors return dispatch info.",
        parameters=[
            OpenApiParameter(
                "limit", int, OpenApiParameter.QUERY, required=False, description="Sample size (default 10)"
            )
        ],
        responses={200: FeedTestResponse, 404: None, 400: None},
    )
    def test_feed(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            limit = int(request.query_params.get("limit", "10"))
        except ValueError as exc:
            raise drf_exceptions.ValidationError(str(exc) or "limit must be int") from exc
        try:
            result = feed_service.test_feed(source_idx, idx, limit=limit)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Feed not found") from exc
        if isinstance(result, list):
            return Response({"is_async": False, "raw_products": result})
        return Response({"is_async": True, **result})
