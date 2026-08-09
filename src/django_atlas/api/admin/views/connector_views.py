# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API view listing discovered connectors."""

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from django_atlas.api.admin.permissions import IsAdminUser
from django_atlas.schemas.responses.connector import ConnectorListResponse
from django_atlas.services import connector_registry

_TAGS = ["Source Connectors"]


class ConnectorListView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAdminUser]

    @extend_schema(tags=_TAGS, summary="List discovered connectors", responses={200: ConnectorListResponse})
    def get(self, request: Request) -> Response:
        items = connector_registry.list_connectors()
        results = [
            {"kind": item["kind"], "name": item["kind"].replace("_", " ").title(), "is_async": item["is_async"]}
            for item in items
        ]
        return Response({"results": results})
