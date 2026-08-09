# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel

from django_atlas.schemas.contract import PriceStockUpdate, RawProduct


class BaseConnector(ABC):
    connector_kind: ClassVar[str]
    is_async: ClassVar[bool]

    @classmethod
    @abstractmethod
    def config_schema(cls) -> type[BaseModel]:
        """Pydantic model used to validate ``feed.feed_config`` for this connector."""

    def validate_config(self, feed_config: dict) -> None:
        self.config_schema().model_validate(feed_config)

    # --- Lifecycle hooks — no-op defaults so
    # xml_feed/scraper and any pre-existing connector keep working unchanged. ---

    def rate_limit_delay(self) -> float | None:
        """Seconds to sleep between fetch batches. None = no rate limiting."""
        return None

    def batch_size(self) -> int | None:
        """Rows per fetch batch. None = orchestration's own default batch size."""
        return None

    def should_retry(self, exc: Exception, attempt: int) -> bool:  # noqa: ARG002 — contract signature
        """Whether the orchestration should retry a failed batch. Default: never."""
        return False

    def before_fetch(self, ctx: dict) -> None:  # noqa: B027 — intentional no-op hook
        """Called once before the fetch loop starts. ``ctx`` = {feed, mode, run_id}."""

    def after_fetch(self, ctx: dict) -> None:  # noqa: B027 — intentional no-op hook
        """Called once after a successful fetch loop. ``ctx`` = {feed, mode, run_id}."""


class SyncConnector(BaseConnector):
    is_async: ClassVar[bool] = False

    @abstractmethod
    def fetch(self, feed) -> Iterator[RawProduct]:
        """Yield RawProduct for every entry in feed (full sync)."""

    @abstractmethod
    def fetch_sample(self, feed, limit: int) -> list[RawProduct]:
        """Return at most ``limit`` RawProduct (init-test)."""

    @abstractmethod
    def fetch_delta(self, feed) -> Iterator[PriceStockUpdate]:
        """Yield PriceStockUpdate for every changed entry (delta sync)."""


class AsyncConnector(BaseConnector):
    is_async: ClassVar[bool] = True

    @abstractmethod
    def dispatch_fetch(self, feed, run_id: UUID) -> str | None:
        """Dispatch full sync to remote worker. Returns task id or None when suppressed."""

    @abstractmethod
    def dispatch_fetch_sample(self, feed, run_id: UUID, limit: int) -> str | None:
        """Dispatch test sync to remote worker."""

    @abstractmethod
    def dispatch_fetch_delta(self, feed, run_id: UUID) -> str | None:
        """Dispatch delta sync to remote worker."""
