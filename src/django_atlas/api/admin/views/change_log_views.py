# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for SourceProductChangeLog read API.

Four endpoints, all under `/api/atlas/v2/admin/pim-sku/`:

  GET  pim-sku/{sku}/changes/         → timeline for one PIM SKU
  GET  pim-sku/has-changes/?skus=...  → bulk badge lookup (throttled)
  POST pim-sku/{sku}/acknowledge/     → mark unseen audit rows as applied
  POST pim-sku/{sku}/force-repush/    → wrapper around push_service per linked SP

C4: ZERO Django models imported here — all ORM via change_log_service.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pydantic import ValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework import viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.api.admin.throttling import BulkHasChangesThrottle
from django_atlas.api.admin.views._helpers import raise_as_drf
from django_atlas.schemas.requests.change_log import AcknowledgeRequest
from django_atlas.schemas.requests.primary import ResetPrimaryToAutoRequest, SetPrimarySourceRequest
from django_atlas.schemas.responses.change_log import (
    AcknowledgeResponse,
    BulkHasChangesResponse,
    ForceRepushBySkuResponse,
    SkuChangesResponse,
)
from django_atlas.schemas.responses.primary import ResetPrimaryToAutoResponse, SetPrimarySourceResponse
from django_atlas.services import change_log_service, primary_strategy_service, product_link_service

_TAGS = ["PIM SKU Bridge"]
_MAX_BULK_SKUS = 100


class PimSkuChangeLogViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    serializer_class = None

    def get_throttles(self):
        if self.action == "has_changes_bulk":
            return [BulkHasChangesThrottle()]
        return []

    @extend_schema(
        tags=_TAGS,
        summary="Audit log timeline for a PIM SKU",
        parameters=[
            OpenApiParameter(
                "since",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="ISO 8601 datetime; only include rows with created_at >= since",
            ),
            OpenApiParameter(
                "source",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="Comma-separated ChangeLogSource values to keep",
            ),
            OpenApiParameter(
                "unseen_only",
                bool,
                OpenApiParameter.QUERY,
                required=False,
                description="If true, only return rows with applied_to_pim=false",
            ),
        ],
        responses={200: SkuChangesResponse, 404: None},
    )
    def changes_for_sku(self, request: Request, sku: str) -> Response:
        since = _parse_iso(request.query_params.get("since"))
        sources = _parse_csv(request.query_params.get("source"))
        unseen_only = _parse_bool(request.query_params.get("unseen_only"))
        try:
            payload = change_log_service.list_for_sku(sku, since=since, sources=sources, unseen_only=unseen_only)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(SkuChangesResponse.model_validate(payload).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Bulk has-changes lookup (PIM list view badge)",
        parameters=[
            OpenApiParameter(
                "skus", str, OpenApiParameter.QUERY, required=True, description="Comma-separated PIM SKUs (max 100)"
            )
        ],
        responses={200: BulkHasChangesResponse, 400: None},
    )
    def has_changes_bulk(self, request: Request) -> Response:
        raw = request.query_params.get("skus") or ""
        skus = [s.strip() for s in raw.split(",") if s.strip()]
        if not skus:
            raise drf_exceptions.ValidationError("Query parameter 'skus' is required.")
        if len(skus) > _MAX_BULK_SKUS:
            raise drf_exceptions.ValidationError(f"Too many SKUs (max {_MAX_BULK_SKUS}).")
        payload = {"skus": change_log_service.bulk_has_changes(skus)}
        return Response(BulkHasChangesResponse.model_validate(payload).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Acknowledge unseen audit rows for a SKU",
        request=AcknowledgeRequest,
        responses={200: AcknowledgeResponse, 400: None, 404: None},
    )
    def acknowledge(self, request: Request, sku: str) -> Response:
        try:
            data = AcknowledgeRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            count = change_log_service.acknowledge(
                sku, change_ids=data.change_ids, all_unseen=data.all_unseen, user=request.user
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(
            AcknowledgeResponse.model_validate({"sku": sku, "acknowledged_count": count}).model_dump(mode="json")
        )

    @extend_schema(
        tags=_TAGS,
        summary="Force re-push every active linked SP for a SKU",
        responses={200: ForceRepushBySkuResponse, 400: None, 404: None},
    )
    def force_repush_by_sku(self, request: Request, sku: str) -> Response:
        try:
            payload = change_log_service.force_repush_by_sku(sku, request.user)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(ForceRepushBySkuResponse.model_validate(payload).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Force-set the primary source for a PIM SKU (manual override)",
        request=SetPrimarySourceRequest,
        responses={200: SetPrimarySourceResponse, 400: None, 404: None},
    )
    def set_primary_source(self, request: Request, sku: str) -> Response:
        try:
            payload = SetPrimarySourceRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        event_sink: list[dict] = []
        previous = product_link_service.get_primary_source_idx(sku)
        try:
            new_link = primary_strategy_service.force_set_primary(
                real_product_sku=sku,
                source_idx=payload.source_idx,
                reason=payload.reason,
                triggered_by=request.user,
                event_sink=event_sink,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        body = {
            "real_product_sku": sku,
            "primary_source_idx": new_link.source.idx,
            "previous_primary_source_idx": previous,
            "manual_override": True,
            "events": event_sink,
        }
        return Response(SetPrimarySourceResponse.model_validate(body).model_dump(mode="json"))

    @extend_schema(
        tags=_TAGS,
        summary="Clear manual override and re-run auto-primary evaluation",
        request=ResetPrimaryToAutoRequest,
        responses={200: ResetPrimaryToAutoResponse, 400: None, 404: None},
    )
    def reset_primary_to_auto(self, request: Request, sku: str) -> Response:
        try:
            ResetPrimaryToAutoRequest(**(request.data or {}))
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        if not product_link_service.has_active_link(sku):
            raise drf_exceptions.NotFound(f"No SourceProductLink exists for sku '{sku}'.")
        event_sink: list[dict] = []
        previous = product_link_service.get_primary_source_idx(sku)
        try:
            result = primary_strategy_service.reset_to_auto(
                real_product_sku=sku, triggered_by=request.user, event_sink=event_sink
            )
        except ValueError as exc:
            raise_as_drf(exc)
        body = {
            "real_product_sku": sku,
            "previous_primary_source_idx": previous,
            "new_primary_source_idx": result.new_primary.source.idx if result.new_primary else None,
            "switched": result.should_switch,
            "skip_reason": result.skip_reason.value,
            "events": event_sink,
        }
        return Response(ResetPrimaryToAutoResponse.model_validate(body).model_dump(mode="json"))


def _parse_iso(value: str | None):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise drf_exceptions.ValidationError(f"'since' is not a valid ISO 8601 datetime: {value}") from exc


def _parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_bool(value: str | None) -> bool:
    if not value:
        return False
    return value.lower() in {"1", "true", "yes"}
