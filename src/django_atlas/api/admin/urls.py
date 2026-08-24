# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin API URL routing — manual `path()` per Volkanos convention."""

from django.urls import path

from django_atlas.api.admin.views.change_log_views import PimSkuChangeLogViewSet
from django_atlas.api.admin.views.competitor_views import CompetitorViewSet
from django_atlas.api.admin.views.connector_views import ConnectorListView
from django_atlas.api.admin.views.feed_views import SourceFeedViewSet
from django_atlas.api.admin.views.log_views import ImportLogViewSet, IntegrationEventViewSet
from django_atlas.api.admin.views.mapping_views import (
    SourceAttributeMappingViewSet,
    SourceCategoryMappingViewSet,
    SourceMappingProfileViewSet,
)
from django_atlas.api.admin.views.observation_views import ObservationViewSet
from django_atlas.api.admin.views.product_link_views import SourceProductLinkViewSet
from django_atlas.api.admin.views.product_views import SourceProductViewSet
from django_atlas.api.admin.views.push_views import BulkPushView
from django_atlas.api.admin.views.realproduct_views import RealProductCrossSourceViewSet
from django_atlas.api.admin.views.settings_views import SourceSettingsView
from django_atlas.api.admin.views.source_views import SourceCredentialsView, SourceViewSet
from django_atlas.api.admin.views.supplier_views import SupplierViewSet

urlpatterns = [
    # Sources
    path("sources/", SourceViewSet.as_view({"get": "list", "post": "create"}), name="admin-sources-list"),
    path(
        "sources/<slug:idx>/",
        SourceViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-sources-detail",
    ),
    path(
        "sources/<slug:idx>/delete-impact/",
        SourceViewSet.as_view({"get": "delete_impact"}),
        name="admin-sources-delete-impact",
    ),
    path("sources/<slug:idx>/data-keys/", SourceViewSet.as_view({"get": "data_keys"}), name="admin-sources-data-keys"),
    path(
        "sources/<slug:idx>/data-values/",
        SourceViewSet.as_view({"get": "data_values"}),
        name="admin-sources-data-values",
    ),
    path(
        "sources/<slug:idx>/credentials/",
        SourceCredentialsView.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-sources-credentials",
    ),
    # Facades — Supplier (procurement) / Competitor (monitoring) projections of Source
    # . Nested resources stay under sources/{idx}/... only.
    path("suppliers/", SupplierViewSet.as_view({"get": "list", "post": "create"}), name="admin-suppliers-list"),
    path(
        "suppliers/<slug:idx>/",
        SupplierViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-suppliers-detail",
    ),
    path("competitors/", CompetitorViewSet.as_view({"get": "list", "post": "create"}), name="admin-competitors-list"),
    path(
        "competitors/<slug:idx>/",
        CompetitorViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-competitors-detail",
    ),
    # Feeds (nested under source)
    path(
        "sources/<slug:source_idx>/feeds/",
        SourceFeedViewSet.as_view({"get": "list", "post": "create"}),
        name="admin-feeds-list",
    ),
    path(
        "sources/<slug:source_idx>/feeds/<slug:idx>/",
        SourceFeedViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-feeds-detail",
    ),
    path(
        "sources/<slug:source_idx>/feeds/<slug:idx>/trigger/",
        SourceFeedViewSet.as_view({"post": "trigger"}),
        name="admin-feeds-trigger",
    ),
    path(
        "sources/<slug:source_idx>/feeds/<slug:idx>/test/",
        SourceFeedViewSet.as_view({"post": "test_feed"}),
        name="admin-feeds-test",
    ),
    # Mapping profiles (nested under source)
    path(
        "sources/<slug:source_idx>/mapping-profiles/",
        SourceMappingProfileViewSet.as_view({"get": "list", "post": "create"}),
        name="admin-mapping-profiles-list",
    ),
    path(
        "sources/<slug:source_idx>/mapping-profiles/<slug:idx>/",
        SourceMappingProfileViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-mapping-profiles-detail",
    ),
    path(
        "sources/<slug:source_idx>/mapping-profiles/<slug:idx>/validate/",
        SourceMappingProfileViewSet.as_view({"post": "validate"}),
        name="admin-mapping-profiles-validate",
    ),
    # Attribute mappings (nested under profile by PK)
    path(
        "mapping-profiles/<int:profile_pk>/attribute-mappings/",
        SourceAttributeMappingViewSet.as_view({"get": "list", "post": "create"}),
        name="admin-attribute-mappings-list",
    ),
    path(
        "mapping-profiles/<int:profile_pk>/attribute-mappings/<int:pk>/",
        SourceAttributeMappingViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-attribute-mappings-detail",
    ),
    # Category mappings (nested under profile by PK)
    path(
        "mapping-profiles/<int:profile_pk>/category-mappings/",
        SourceCategoryMappingViewSet.as_view({"get": "list", "post": "create"}),
        name="admin-category-mappings-list",
    ),
    path(
        "mapping-profiles/<int:profile_pk>/category-mappings/<int:pk>/",
        SourceCategoryMappingViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-category-mappings-detail",
    ),
    # Products (cross-source)
    path("products/", SourceProductViewSet.as_view({"get": "list"}), name="admin-products-list"),
    path(
        "products/bulk-approve/",
        SourceProductViewSet.as_view({"post": "bulk_approve"}),
        name="admin-products-bulk-approve",
    ),
    path(
        "products/bulk-reject/",
        SourceProductViewSet.as_view({"post": "bulk_reject"}),
        name="admin-products-bulk-reject",
    ),
    path(
        "products/bulk-requeue/",
        SourceProductViewSet.as_view({"post": "bulk_requeue"}),
        name="admin-products-bulk-requeue",
    ),
    path(
        "products/<int:pk>/",
        SourceProductViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-products-detail",
    ),
    path(
        "products/<int:pk>/approve/", SourceProductViewSet.as_view({"post": "approve"}), name="admin-products-approve"
    ),
    path("products/<int:pk>/reject/", SourceProductViewSet.as_view({"post": "reject"}), name="admin-products-reject"),
    path("products/<int:pk>/skip/", SourceProductViewSet.as_view({"post": "skip"}), name="admin-products-skip"),
    path("products/<int:pk>/queue/", SourceProductViewSet.as_view({"post": "queue"}), name="admin-products-queue"),
    path("products/<int:pk>/push/", SourceProductViewSet.as_view({"post": "push"}), name="admin-products-push"),
    path(
        "products/<int:pk>/force-repush/",
        SourceProductViewSet.as_view({"post": "force_repush"}),
        name="admin-products-force-repush",
    ),
    path(
        "products/<int:pk>/link-to-realproduct/",
        SourceProductViewSet.as_view({"post": "link_to_realproduct"}),
        name="admin-products-link-to-realproduct",
    ),
    path(
        "products/<int:pk>/unlink-from-realproduct/",
        SourceProductViewSet.as_view({"post": "unlink_from_realproduct"}),
        name="admin-products-unlink-from-realproduct",
    ),
    # Product Source Links (cross-source)
    path(
        "product-links/",
        SourceProductLinkViewSet.as_view({"get": "list", "post": "create"}),
        name="admin-product-links-list",
    ),
    path(
        "product-links/<int:pk>/",
        SourceProductLinkViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="admin-product-links-detail",
    ),
    path(
        "product-links/<int:pk>/set-primary/",
        SourceProductLinkViewSet.as_view({"post": "set_primary"}),
        name="admin-product-links-set-primary",
    ),
    # Logs + Events
    path("import-logs/", ImportLogViewSet.as_view({"get": "list"}), name="admin-import-logs-list"),
    path("import-logs/<int:pk>/", ImportLogViewSet.as_view({"get": "retrieve"}), name="admin-import-logs-detail"),
    path("events/", IntegrationEventViewSet.as_view({"get": "list"}), name="admin-events-list"),
    path("events/<int:pk>/", IntegrationEventViewSet.as_view({"get": "retrieve"}), name="admin-events-detail"),
    path(
        "events/<int:pk>/acknowledge/",
        IntegrationEventViewSet.as_view({"post": "acknowledge"}),
        name="admin-events-acknowledge",
    ),
    # PIM SKU bridge — audit log read API
    # NOTE: `has-changes/` MUST precede `<path:sku>/...` so the literal slug
    # is not swallowed by the SKU converter.
    path(
        "pim-sku/has-changes/",
        PimSkuChangeLogViewSet.as_view({"get": "has_changes_bulk"}),
        name="admin-pim-sku-has-changes",
    ),
    path(
        "pim-sku/<path:sku>/changes/",
        PimSkuChangeLogViewSet.as_view({"get": "changes_for_sku"}),
        name="admin-pim-sku-changes",
    ),
    path(
        "pim-sku/<path:sku>/acknowledge/",
        PimSkuChangeLogViewSet.as_view({"post": "acknowledge"}),
        name="admin-pim-sku-acknowledge",
    ),
    path(
        "pim-sku/<path:sku>/force-repush/",
        PimSkuChangeLogViewSet.as_view({"post": "force_repush_by_sku"}),
        name="admin-pim-sku-force-repush",
    ),
    # auto-primary selection manual override / reset
    path(
        "pim-sku/<path:sku>/set-primary-source/",
        PimSkuChangeLogViewSet.as_view({"post": "set_primary_source"}),
        name="admin-pim-sku-set-primary-source",
    ),
    path(
        "pim-sku/<path:sku>/reset-primary-to-auto/",
        PimSkuChangeLogViewSet.as_view({"post": "reset_primary_to_auto"}),
        name="admin-pim-sku-reset-primary-to-auto",
    ),
    # RealProducts
    path(
        "realproducts/merge-by-ean/",
        RealProductCrossSourceViewSet.as_view({"post": "merge_by_ean"}),
        name="admin-realproducts-merge-by-ean",
    ),
    path(
        "auto-matched/",
        RealProductCrossSourceViewSet.as_view({"get": "auto_matched_list"}),
        name="admin-auto-matched-list",
    ),
    path(
        "duplicates/", RealProductCrossSourceViewSet.as_view({"get": "duplicates_list"}), name="admin-duplicates-list"
    ),
    # Observations
    path("observations/", ObservationViewSet.as_view({"get": "list"}), name="admin-observations-list"),
    # Push, Settings, Connectors
    path("push/", BulkPushView.as_view(), name="admin-push"),
    path("settings/", SourceSettingsView.as_view(), name="admin-settings"),
    path("connectors/", ConnectorListView.as_view(), name="admin-connectors"),
]
