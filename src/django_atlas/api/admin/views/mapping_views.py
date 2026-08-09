# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API views for SourceMappingProfile + Attribute/Category mappings.

C4: ZERO Django models imported here — all ORM via mapping_service / source_service.
"""

from django_utils.api.v2_errors import raise_pydantic_as_drf
from drf_spectacular.utils import extend_schema
from pydantic import ValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.pagination import AdminPageNumberPagination
from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.api.admin.views._helpers import raise_as_drf
from django_atlas.schemas.requests.mapping import (
    AttributeMappingCreateRequest,
    AttributeMappingUpdateRequest,
    CategoryMappingCreateRequest,
    CategoryMappingUpdateRequest,
    MappingProfileCreateRequest,
    MappingProfileUpdateRequest,
)
from django_atlas.schemas.responses.mapping import (
    AttributeMappingListResponse,
    AttributeMappingResponse,
    CategoryMappingListResponse,
    CategoryMappingResponse,
    MappingProfileListResponse,
    MappingProfileResponse,
    MappingValidationResponse,
)
from django_atlas.services import mapping_service, source_service


def _serialize_profile(p) -> dict:
    return MappingProfileResponse.model_validate(p).model_dump(mode="json")


def _serialize_attr(m) -> dict:
    return AttributeMappingResponse.model_validate(m).model_dump(mode="json")


def _serialize_cat(m) -> dict:
    return CategoryMappingResponse.model_validate(m).model_dump(mode="json")


_TAGS = ["Source Mappings"]


class SourceMappingProfileViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(tags=_TAGS, summary="List mapping profiles", responses={200: MappingProfileListResponse, 404: None})
    def list(self, request: Request, source_idx: str) -> Response:
        if not source_service.source_exists(source_idx):
            raise drf_exceptions.NotFound(f"Source '{source_idx}' not found")
        qs = mapping_service.list_profiles(source_idx)
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        results = [_serialize_profile(p) for p in page]
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": results,
            }
        )

    @extend_schema(tags=_TAGS, summary="Retrieve mapping profile", responses={200: MappingProfileResponse, 404: None})
    def retrieve(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            profile = mapping_service.get_profile(source_idx, idx)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize_profile(profile))

    @extend_schema(
        tags=_TAGS,
        summary="Create mapping profile",
        request=MappingProfileCreateRequest,
        responses={201: MappingProfileResponse, 400: None, 404: None},
    )
    def create(self, request: Request, source_idx: str) -> Response:
        try:
            data = MappingProfileCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            profile = mapping_service.create_profile(
                source_idx,
                idx=data.idx,
                name=data.name,
                target_channel_idxs=data.target_channel_idxs,
                feature_set_idx=data.feature_set_idx,
                is_active=data.is_active,
                import_language_id=data.import_language_id,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize_profile(profile), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update mapping profile",
        request=MappingProfileUpdateRequest,
        responses={200: MappingProfileResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            data = MappingProfileUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        try:
            profile = mapping_service.update_profile(source_idx, idx, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize_profile(profile))

    @extend_schema(tags=_TAGS, summary="Delete mapping profile", responses={204: None, 404: None})
    def destroy(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            mapping_service.delete_profile(source_idx, idx)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=_TAGS,
        summary="Validate mapping profile (PIM coverage check)",
        responses={200: MappingValidationResponse, 404: None},
    )
    def validate(self, request: Request, source_idx: str, idx: str) -> Response:
        try:
            result = mapping_service.validate_profile(source_idx, idx)
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(result)


class SourceAttributeMappingViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List attribute mappings for profile",
        responses={200: AttributeMappingListResponse, 404: None},
    )
    def list(self, request: Request, profile_pk: int) -> Response:
        try:
            mapping_service.get_profile_by_pk(profile_pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Profile not found") from exc
        qs = mapping_service.list_attribute_mappings(profile_pk)
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": [_serialize_attr(m) for m in page],
            }
        )

    @extend_schema(
        tags=_TAGS, summary="Retrieve attribute mapping", responses={200: AttributeMappingResponse, 404: None}
    )
    def retrieve(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            mapping = mapping_service.get_attribute_mapping(profile_pk, pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Attribute mapping not found") from exc
        return Response(_serialize_attr(mapping))

    @extend_schema(
        tags=_TAGS,
        summary="Create attribute mapping",
        request=AttributeMappingCreateRequest,
        responses={201: AttributeMappingResponse, 400: None, 404: None},
    )
    def create(self, request: Request, profile_pk: int) -> Response:
        try:
            data = AttributeMappingCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            profile = mapping_service.get_profile_by_pk(profile_pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Profile not found") from exc
        try:
            mapping = mapping_service.add_attribute_mapping(
                profile,
                source_field=data.source_field,
                target_type=data.target_type,
                target_identifier=data.target_identifier,
                is_required=data.is_required,
                modifier=data.modifier,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize_attr(mapping), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update attribute mapping",
        request=AttributeMappingUpdateRequest,
        responses={200: AttributeMappingResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            data = AttributeMappingUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        try:
            mapping = mapping_service.update_attribute_mapping(pk, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        if mapping.profile_id != profile_pk:
            raise drf_exceptions.NotFound("Attribute mapping not found")
        return Response(_serialize_attr(mapping))

    @extend_schema(tags=_TAGS, summary="Delete attribute mapping", responses={204: None, 404: None})
    def destroy(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            mapping = mapping_service.get_attribute_mapping(profile_pk, pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Attribute mapping not found") from exc
        mapping_service.remove_attribute_mapping(mapping.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SourceCategoryMappingViewSet(viewsets.ViewSet):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]
    pagination_class = AdminPageNumberPagination
    serializer_class = None

    @extend_schema(
        tags=_TAGS,
        summary="List category mappings for profile",
        responses={200: CategoryMappingListResponse, 404: None},
    )
    def list(self, request: Request, profile_pk: int) -> Response:
        try:
            mapping_service.get_profile_by_pk(profile_pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Profile not found") from exc
        qs = mapping_service.list_category_mappings(profile_pk)
        paginator = AdminPageNumberPagination()
        page = paginator.paginate_queryset(qs, request)
        return Response(
            {
                "count": qs.count(),
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": [_serialize_cat(m) for m in page],
            }
        )

    @extend_schema(tags=_TAGS, summary="Retrieve category mapping", responses={200: CategoryMappingResponse, 404: None})
    def retrieve(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            mapping = mapping_service.get_category_mapping(profile_pk, pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Category mapping not found") from exc
        return Response(_serialize_cat(mapping))

    @extend_schema(
        tags=_TAGS,
        summary="Create category mapping",
        request=CategoryMappingCreateRequest,
        responses={201: CategoryMappingResponse, 400: None, 404: None},
    )
    def create(self, request: Request, profile_pk: int) -> Response:
        try:
            data = CategoryMappingCreateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        try:
            profile = mapping_service.get_profile_by_pk(profile_pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Profile not found") from exc
        try:
            mapping = mapping_service.add_category_mapping(
                profile,
                source_field=data.source_field,
                source_value=data.source_value,
                target_category_idx=data.target_category_idx,
            )
        except ValueError as exc:
            raise_as_drf(exc)
        return Response(_serialize_cat(mapping), status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=_TAGS,
        summary="Partial update category mapping",
        request=CategoryMappingUpdateRequest,
        responses={200: CategoryMappingResponse, 400: None, 404: None},
    )
    def partial_update(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            data = CategoryMappingUpdateRequest(**request.data)
        except ValidationError as exc:
            raise_pydantic_as_drf(exc)
        update_kwargs = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
        try:
            mapping = mapping_service.update_category_mapping(pk, **update_kwargs)
        except ValueError as exc:
            raise_as_drf(exc)
        if mapping.profile_id != profile_pk:
            raise drf_exceptions.NotFound("Category mapping not found")
        return Response(_serialize_cat(mapping))

    @extend_schema(tags=_TAGS, summary="Delete category mapping", responses={204: None, 404: None})
    def destroy(self, request: Request, profile_pk: int, pk: int) -> Response:
        try:
            mapping = mapping_service.get_category_mapping(profile_pk, pk)
        except ValueError as exc:
            raise drf_exceptions.NotFound(str(exc) or "Category mapping not found") from exc
        mapping_service.remove_category_mapping(mapping.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)
