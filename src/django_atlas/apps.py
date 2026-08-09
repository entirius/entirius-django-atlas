# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from django.apps import AppConfig
from django.db import transaction
from django.db.models.signals import post_migrate, post_save


def _ensure_settings_singleton(sender, **kwargs) -> None:
    if sender.name != "django_atlas":
        return
    from django_atlas.models import SourceSettings

    SourceSettings.objects.get_or_create(pk=1)


def _invalidate_settings_cache(sender, instance, **kwargs) -> None:
    """Invalidate the auto_push_enabled cache when SourceSettings changes.

    Decision #32: cache deletion runs via `transaction.on_commit` so a rollback
    after the post_save signal does not leave the cache cleared while the DB
    keeps the old value. Without this, eventually-consistent state persists for
    up to 60s (cache TTL).
    """
    from django_atlas.signals.killswitch import invalidate_auto_push_cache

    transaction.on_commit(invalidate_auto_push_cache)


class DjangoAtlasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_atlas"
    verbose_name = "Sources"
    is_volkanos = True

    def ready(self) -> None:
        from django_atlas.models import SourceSettings
        from django_atlas.signals.definitions import source_product_pushed_signal, source_products_imported_signal
        from django_atlas.signals.handlers import on_source_product_pushed, on_source_products_imported

        post_migrate.connect(_ensure_settings_singleton, sender=self)
        post_save.connect(
            _invalidate_settings_cache, sender=SourceSettings, dispatch_uid="django_atlas.invalidate_settings_cache"
        )
        source_product_pushed_signal.connect(on_source_product_pushed, dispatch_uid="django_atlas.image_dispatch")
        source_products_imported_signal.connect(on_source_products_imported, dispatch_uid="django_atlas.auto_push")
