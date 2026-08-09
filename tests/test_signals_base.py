# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import threading

import pytest
from django.core.cache import cache

from django_atlas.enums import EventType
from django_atlas.models import SourceSettings
from django_atlas.signals import (
    cost_updated_signal,
    is_auto_push_enabled,
    is_suppressed,
    source_product_pushed_signal,
    source_products_imported_signal,
    suppress_source_signals,
)
from django_atlas.signals.killswitch import _AUTO_PUSH_CACHE_KEY

# ---------------------------------------------------------------------------
# 1. Signal exports importable
# ---------------------------------------------------------------------------


def test_signal_exports_importable():
    assert source_products_imported_signal is not None
    assert cost_updated_signal is not None
    assert source_product_pushed_signal is not None


# ---------------------------------------------------------------------------
# 2. suppress_source_signals + is_suppressed
# ---------------------------------------------------------------------------


def test_suppress_context_toggles_is_suppressed():
    assert is_suppressed() is False
    with suppress_source_signals():
        assert is_suppressed() is True
    assert is_suppressed() is False


# ---------------------------------------------------------------------------
# 3. Thread-local isolation
# ---------------------------------------------------------------------------


def test_suppress_is_thread_local():
    states: dict[str, bool | None] = {"a": None, "b": None}
    barrier = threading.Barrier(2)

    def thread_a():
        with suppress_source_signals():
            barrier.wait()  # ensure thread_b reads while we are suppressed
            states["a"] = is_suppressed()
            barrier.wait()  # let thread_b record its observation before we exit

    def thread_b():
        barrier.wait()  # wait until thread_a is inside suppress
        states["b"] = is_suppressed()
        barrier.wait()

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert states["a"] is True
    assert states["b"] is False


# ---------------------------------------------------------------------------
# 4. is_auto_push_enabled default True
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_is_auto_push_enabled_default_true():
    cache.delete(_AUTO_PUSH_CACHE_KEY)
    SourceSettings.objects.update_or_create(pk=1, defaults={"auto_push_enabled": True})

    assert is_auto_push_enabled() is True


# ---------------------------------------------------------------------------
# 5. Cache invalidation via post_save signal (transaction.on_commit)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_is_auto_push_enabled_cache_invalidated_on_settings_save():
    cache.delete(_AUTO_PUSH_CACHE_KEY)
    settings = SourceSettings.load()
    settings.auto_push_enabled = True
    settings.save()
    assert is_auto_push_enabled() is True

    settings.auto_push_enabled = False
    settings.save()

    assert is_auto_push_enabled() is False


# ---------------------------------------------------------------------------
# +1 enum sanity — channel_removed_from_profile EventType present
# ---------------------------------------------------------------------------


def test_event_type_includes_channel_removed_from_profile():
    assert EventType.CHANNEL_REMOVED_FROM_PROFILE.value == "channel_removed_from_profile"
    assert "channel_removed_from_profile" in {choice[0] for choice in EventType.choices}
