# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from typing import ClassVar
from uuid import UUID

from celery import current_app
from pydantic import BaseModel, ConfigDict

from django_atlas.connectors.base import AsyncConnector
from django_atlas.enums import EventSeverity, EventType
from django_atlas.models import SourceSettings
from django_atlas.services import event_service

_TASK_NAME = "source.scraper.fetch"
_QUEUE = "scraping"


class ScraperConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scraper_id: str
    catalog_url: str
    proxy: str | None = None
    delay_min: int = 1
    delay_max: int = 3
    impersonate: str | None = "chrome131"


class ScraperConnector(AsyncConnector):
    connector_kind: ClassVar[str] = "scraper"

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return ScraperConfig

    def _check_killswitch(self, feed) -> bool:
        settings = SourceSettings.load()
        if settings.scraper_dispatch_enabled:
            return True
        event_service.record(
            event_type=EventType.SCRAPER_DISPATCH_SKIPPED.value,
            severity=EventSeverity.WARNING.value,
            feed=feed,
            source=feed.source,
            message="Dispatch suppressed by SourceSettings.scraper_dispatch_enabled killswitch",
        )
        return False

    def _send(self, feed, run_id: UUID, mode: str, limit: int | None) -> str:
        cfg = ScraperConfig.model_validate(feed.feed_config)
        result = current_app.send_task(
            _TASK_NAME,
            args=[],
            kwargs={
                "scraper_id": cfg.scraper_id,
                "feed_config": dict(feed.feed_config),
                "run_id": str(run_id),
                "mode": mode,
                "limit": limit,
            },
            queue=_QUEUE,
            retry=False,
        )
        return getattr(result, "id", str(result))

    def dispatch_fetch(self, feed, run_id: UUID) -> str | None:
        if not self._check_killswitch(feed):
            return None
        return self._send(feed, run_id, mode="full", limit=None)

    def dispatch_fetch_sample(self, feed, run_id: UUID, limit: int) -> str | None:
        if not self._check_killswitch(feed):
            return None
        return self._send(feed, run_id, mode="test", limit=limit)

    def dispatch_fetch_delta(self, feed, run_id: UUID) -> str | None:
        if not self._check_killswitch(feed):
            return None
        return self._send(feed, run_id, mode="delta", limit=None)
