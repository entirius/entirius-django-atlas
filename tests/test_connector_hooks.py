# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the connector lifecycle hooks.

Covers the hook *contract* (BaseConnector no-op defaults) and the orchestration's use of
them in `import_service`: batch_size, rate_limit_delay, should_retry (+ hard cap), and
before_fetch/after_fetch. Uses a small real `SyncConnector` subclass rather than
`MagicMock(spec=...)` — a bare mock's `batch_size()`/`rate_limit_delay()` return a truthy
MagicMock instead of None, which silently breaks batching (see test_import_service.py's
`_sync_connector_mock` for the mock-based tests that still need patching).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from django_atlas.connectors.base import BaseConnector, SyncConnector
from django_atlas.models import SourceProduct
from django_atlas.schemas.contract import RawProduct
from django_atlas.services import connector_registry, import_service, log_service
from tests.factories import FeedFactory, SourceFactory, SourceProductFactory

pytestmark = pytest.mark.django_db


class _EmptyConfig(BaseModel):
    pass


class _HookConnector(SyncConnector):
    """Minimal real connector exercising every lifecycle hook, with call tracking."""

    connector_kind = "hook-test"

    def __init__(
        self,
        raws: list[RawProduct] | None = None,
        deltas: list | None = None,
        batch_size: int | None = None,
        rate_limit_delay: float | None = None,
        should_retry_result: bool = False,
        fetch_raises: Exception | None = None,
    ) -> None:
        self._raws = raws or []
        self._deltas = deltas or []
        self._batch_size = batch_size
        self._rate_limit_delay = rate_limit_delay
        self._should_retry_result = should_retry_result
        self._fetch_raises = fetch_raises
        self.before_calls: list[dict] = []
        self.after_calls: list[dict] = []
        self.should_retry_calls: list[int] = []

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return _EmptyConfig

    def fetch(self, feed):  # noqa: ARG002
        if self._fetch_raises is not None:
            raise self._fetch_raises
        return iter(self._raws)

    def fetch_sample(self, feed, limit):  # noqa: ARG002
        return self._raws[:limit]

    def fetch_delta(self, feed):  # noqa: ARG002
        return iter(self._deltas)

    def batch_size(self) -> int | None:
        return self._batch_size

    def rate_limit_delay(self) -> float | None:
        return self._rate_limit_delay

    def should_retry(self, exc: Exception, attempt: int) -> bool:  # noqa: ARG002
        self.should_retry_calls.append(attempt)
        return self._should_retry_result

    def before_fetch(self, ctx: dict) -> None:
        self.before_calls.append(ctx)

    def after_fetch(self, ctx: dict) -> None:
        self.after_calls.append(ctx)


class _BareConnector(SyncConnector):
    """No hook overrides at all — exercises BaseConnector's no-op defaults."""

    connector_kind = "bare-test"

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return _EmptyConfig

    def fetch(self, feed):  # noqa: ARG002
        return iter([])

    def fetch_sample(self, feed, limit):  # noqa: ARG002
        return []

    def fetch_delta(self, feed):  # noqa: ARG002
        return iter([])


# ============== Hook contract defaults ==============


def test_default_batch_size_is_none():
    assert _BareConnector().batch_size() is None


def test_default_rate_limit_delay_is_none():
    assert _BareConnector().rate_limit_delay() is None


def test_default_should_retry_is_false():
    assert _BareConnector().should_retry(RuntimeError("x"), 1) is False


def test_default_before_after_fetch_are_noop():
    connector = _BareConnector()
    assert connector.before_fetch({"feed": None, "mode": "full", "run_id": "x"}) is None
    assert connector.after_fetch({"feed": None, "mode": "full", "run_id": "x"}) is None


def test_hooks_do_not_require_abstractmethod_override(monkeypatch):
    """Gotcha: hooks must NOT be @abstractmethod, or connectors without an override
    stop instantiating (breaks every pre-existing connector, e.g. xml_feed/scraper)."""
    assert not getattr(BaseConnector.rate_limit_delay, "__isabstractmethod__", False)
    assert not getattr(BaseConnector.batch_size, "__isabstractmethod__", False)
    assert not getattr(BaseConnector.should_retry, "__isabstractmethod__", False)
    assert not getattr(BaseConnector.before_fetch, "__isabstractmethod__", False)
    assert not getattr(BaseConnector.after_fetch, "__isabstractmethod__", False)
    _BareConnector()  # must not raise


# ============== batch_size hook respected by orchestration ==============


def test_batch_size_hook_splits_fetch_into_smaller_batches():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    raws = [RawProduct(external_id=f"B-{n}", name=f"P{n}") for n in range(5)]
    connector = _HookConnector(raws=raws, batch_size=2)
    log = log_service.start_import_log(feed, mode="full")
    with patch.object(SourceProduct.objects, "bulk_create", wraps=SourceProduct.objects.bulk_create) as spy:
        counts = import_service.process_full_sync(feed, connector, log)
    assert counts["new_count"] == 5
    assert spy.call_count == 3  # ceil(5/2)


# ============== rate_limit_delay hook respected by orchestration ==============


def test_rate_limit_delay_hook_sleeps_between_batches():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    raws = [RawProduct(external_id=f"R-{n}", name=f"P{n}") for n in range(3)]
    connector = _HookConnector(raws=raws, batch_size=1, rate_limit_delay=0.01)
    log = log_service.start_import_log(feed, mode="full")
    with patch("django_atlas.services.import_service.time.sleep") as sleep_spy:
        import_service.process_full_sync(feed, connector, log)
    assert sleep_spy.call_count == 3
    sleep_spy.assert_called_with(0.01)


def test_no_rate_limit_delay_never_sleeps():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    raws = [RawProduct(external_id="R-1", name="P1")]
    connector = _HookConnector(raws=raws)
    log = log_service.start_import_log(feed, mode="full")
    with patch("django_atlas.services.import_service.time.sleep") as sleep_spy:
        import_service.process_full_sync(feed, connector, log)
    sleep_spy.assert_not_called()


# ============== before_fetch / after_fetch ctx ==============


def test_before_and_after_fetch_called_with_ctx(monkeypatch):
    source = SourceFactory()
    feed = FeedFactory(source=source)
    connector = _HookConnector(raws=[RawProduct(external_id="X-1", name="X")])
    monkeypatch.setattr(connector_registry, "get_connector", lambda kind: connector)  # noqa: ARG005
    log = import_service.execute_feed(feed)
    assert len(connector.before_calls) == 1
    assert len(connector.after_calls) == 1
    ctx = connector.before_calls[0]
    assert ctx["feed"] == feed
    assert ctx["mode"] == "full"
    assert ctx["run_id"] == log.run_id
    assert connector.after_calls[0] == ctx


def test_after_fetch_not_called_when_fetch_fails(monkeypatch):
    source = SourceFactory()
    feed = FeedFactory(source=source)
    connector = _HookConnector(fetch_raises=RuntimeError("boom"))
    monkeypatch.setattr(connector_registry, "get_connector", lambda kind: connector)  # noqa: ARG005
    import_service.execute_feed(feed)
    assert len(connector.before_calls) == 1
    assert connector.after_calls == []


# ============== should_retry hook + hard cap ==============


def test_should_retry_false_raises_immediately_no_retry():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    connector = _HookConnector(raws=[RawProduct(external_id="X-1", name="X")], should_retry_result=False)
    log = log_service.start_import_log(feed, mode="full")
    calls = {"n": 0}

    def _always_fail(*args, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("boom")

    with patch.object(SourceProduct.objects, "bulk_create", side_effect=_always_fail):
        with pytest.raises(RuntimeError, match="boom"):
            import_service.process_full_sync(feed, connector, log)
    assert calls["n"] == 1
    assert connector.should_retry_calls == [1]


def test_should_retry_true_retries_until_success():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    connector = _HookConnector(raws=[RawProduct(external_id="X-1", name="X")], should_retry_result=True)
    log = log_service.start_import_log(feed, mode="full")
    real_bulk_create = SourceProduct.objects.bulk_create
    calls = {"n": 0}

    def _fail_twice(objs, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        return real_bulk_create(objs, **kwargs)

    with patch.object(SourceProduct.objects, "bulk_create", side_effect=_fail_twice):
        counts = import_service.process_full_sync(feed, connector, log)
    assert calls["n"] == 3
    assert connector.should_retry_calls == [1, 2]
    assert counts["new_count"] == 1
    assert SourceProduct.objects.filter(source=source, external_id="X-1").count() == 1


def test_should_retry_hard_cap_10_attempts_regardless_of_connector():
    source = SourceFactory()
    feed = FeedFactory(source=source)
    connector = _HookConnector(raws=[RawProduct(external_id="X-1", name="X")], should_retry_result=True)
    log = log_service.start_import_log(feed, mode="full")
    calls = {"n": 0}

    def _always_fail(*args, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("boom")

    with patch.object(SourceProduct.objects, "bulk_create", side_effect=_always_fail):
        with pytest.raises(RuntimeError, match="boom"):
            import_service.process_full_sync(feed, connector, log)
    assert calls["n"] == 10  # hard cap regardless of should_retry always returning True
    assert connector.should_retry_calls == list(range(1, 10))  # attempt 10's failure short-circuits the cap check


def test_batch_retry_rolls_back_partial_writes_no_duplicates():
    """A failed attempt's writes must not leak into the retried attempt."""
    source = SourceFactory()
    feed = FeedFactory(source=source)
    SourceProductFactory(source=source, feed=feed, external_id="EXISTING-1", name="old")

    raws = [RawProduct(external_id="NEW-1", name="new"), RawProduct(external_id="EXISTING-1", name="changed")]
    connector = _HookConnector(raws=raws, batch_size=10, should_retry_result=True)
    log = log_service.start_import_log(feed, mode="full")

    real_bulk_update = SourceProduct.objects.bulk_update
    calls = {"n": 0}

    def _fail_first_update(objs, fields, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB error after bulk_create ran")
        return real_bulk_update(objs, fields, **kwargs)

    with patch.object(SourceProduct.objects, "bulk_update", side_effect=_fail_first_update):
        counts = import_service.process_full_sync(feed, connector, log)

    assert counts["error_count"] == 0
    assert connector.should_retry_calls == [1]
    # The first attempt's bulk_create (NEW-1) must have rolled back with the batch —
    # otherwise this would be 2 (one from each attempt).
    assert SourceProduct.objects.filter(source=source, external_id="NEW-1").count() == 1
    sp = SourceProduct.objects.get(source=source, external_id="EXISTING-1")
    assert sp.name == "changed"


# ============== delta side-effects fire once, post-commit ==============


def _pushed_procurement_sp():
    """Pushed procurement SP with a real_product FK + channel — the full gate chain
    `_write_qms_cost_for_pushed_sp` needs to actually emit `cost_updated_signal`."""
    from decimal import Decimal

    from django_pim.models.real_product import RealProduct

    from django_atlas.enums import ProductStatus

    source = SourceFactory()
    feed = FeedFactory(source=source, sync_mode="delta")
    rp = RealProduct.objects.create(sku="sku-retry-fx")
    sp = SourceProductFactory(
        source=source,
        feed=feed,
        external_id="SKU-FX-1",
        cost=Decimal("100.00"),
        currency="EUR",
        stock=42,
        status=ProductStatus.PUSHED.value,
    )
    sp.real_product = rp
    sp.pushed_to_channel_idxs = ["default"]
    sp.save(update_fields=["real_product", "pushed_to_channel_idxs"])
    return feed


def test_delta_retry_emits_side_effects_exactly_once():
    """A retried batch must emit cost signals / emergency eval ONCE — for the committed
    attempt only. Before the fix they fired inside `transaction.atomic()`, so the failed
    attempt had already emitted them (on data that then rolled back) and the successful
    retry emitted them a second time."""
    from decimal import Decimal

    from django_atlas.schemas.contract import PriceStockUpdate
    from django_atlas.signals import cost_updated_signal

    feed = _pushed_procurement_sp()
    upd = PriceStockUpdate(external_id="SKU-FX-1", cost=Decimal("90.00"), stock=0)
    connector = _HookConnector(deltas=[upd], should_retry_result=True)
    log = log_service.start_import_log(feed, mode="delta")

    real_bulk_update = SourceProduct.objects.bulk_update
    calls = {"n": 0}

    def _fail_first_update(objs, fields, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_bulk_update(objs, fields, **kwargs)

    received: list[dict] = []

    def _receiver(sender, **kwargs):  # noqa: ARG001
        received.append(kwargs)

    cost_updated_signal.connect(_receiver)
    try:
        with (
            patch.object(SourceProduct.objects, "bulk_update", side_effect=_fail_first_update),
            patch(
                "django_atlas.services.primary_strategy_service.maybe_trigger_emergency_on_stock_drop"
            ) as emergency_spy,
        ):
            import_service.process_delta_sync(feed, connector, log)
    finally:
        cost_updated_signal.disconnect(_receiver)

    assert connector.should_retry_calls == [1]
    assert len(received) == 1  # one channel, one committed batch — not one per attempt
    assert received[0]["cost"] == Decimal("90.00")
    assert emergency_spy.call_count == 1


def test_delta_failed_batch_emits_no_side_effects():
    """A batch that never commits must not emit any externally visible side-effect."""
    from decimal import Decimal

    from django_atlas.schemas.contract import PriceStockUpdate
    from django_atlas.signals import cost_updated_signal

    feed = _pushed_procurement_sp()
    upd = PriceStockUpdate(external_id="SKU-FX-1", cost=Decimal("90.00"), stock=0)
    connector = _HookConnector(deltas=[upd], should_retry_result=False)
    log = log_service.start_import_log(feed, mode="delta")

    received: list[dict] = []

    def _receiver(sender, **kwargs):  # noqa: ARG001
        received.append(kwargs)

    cost_updated_signal.connect(_receiver)
    try:
        with (
            patch.object(SourceProduct.objects, "bulk_update", side_effect=RuntimeError("boom")),
            patch(
                "django_atlas.services.primary_strategy_service.maybe_trigger_emergency_on_stock_drop"
            ) as emergency_spy,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                import_service.process_delta_sync(feed, connector, log)
    finally:
        cost_updated_signal.disconnect(_receiver)

    assert received == []
    assert emergency_spy.call_count == 0
