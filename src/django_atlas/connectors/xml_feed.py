# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import ClassVar

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from django_atlas import settings as source_settings
from django_atlas.connectors.base import SyncConnector
from django_atlas.schemas.contract import PriceStockUpdate, RawProduct
from django_atlas.security import safe_get

_DEFAULT_TIMEOUT = 60

# Hardened lxml parser — blocks XXE / billion-laughs / DTD-driven SSRF.
_SAFE_XML_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False
)


def _fetch_capped(url: str) -> bytes:
    """GET ``url`` with SSRF guard + body cap. Raises on HTTP error or oversize."""
    return safe_get(url, timeout=_DEFAULT_TIMEOUT, cap=source_settings.ATLAS_MAX_FEED_BYTES)


class XmlFeedConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    feed_url: str
    product_xpath: str = ".//product"
    field_mapping: dict[str, str] = Field(
        ...,
        description="Mapping {raw_field -> XPath relative to product node}. Required keys: external_id, name, cost.",
    )
    image_xpath: str = "./images/img/@src"
    delta_field_mapping: dict[str, str] | None = None


def _xpath_text(node, expr: str) -> str | None:
    if not expr:
        return None
    result = node.xpath(expr)
    if not result:
        return None
    first = result[0]
    if hasattr(first, "text"):
        text = first.text
    else:
        text = str(first)
    if text is None:
        return None
    text = text.strip()
    return text or None


def _to_decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


class XmlFeedConnector(SyncConnector):
    connector_kind: ClassVar[str] = "xml_feed"

    @classmethod
    def config_schema(cls) -> type[BaseModel]:
        return XmlFeedConfig

    def _build_raw_product(self, node, cfg: XmlFeedConfig) -> RawProduct:
        mapping = cfg.field_mapping
        external_id = _xpath_text(node, mapping.get("external_id", ""))
        name = _xpath_text(node, mapping.get("name", ""))
        if external_id is None or name is None:
            raise ValueError("Missing required external_id or name in XML node")
        attributes: dict[str, str] = {}
        reserved = {"external_id", "name", "cost", "currency", "stock", "ean", "url"}
        for key, expr in mapping.items():
            if key in reserved:
                continue
            value = _xpath_text(node, expr)
            if value is not None:
                attributes[key] = value
        images = [str(s) for s in node.xpath(cfg.image_xpath)] if cfg.image_xpath else []
        return RawProduct(
            external_id=external_id,
            name=name,
            cost=_to_decimal(_xpath_text(node, mapping.get("cost", ""))),
            currency=_xpath_text(node, mapping.get("currency", "")),
            stock=_to_int(_xpath_text(node, mapping.get("stock", ""))),
            ean=_xpath_text(node, mapping.get("ean", "")),
            url=_xpath_text(node, mapping.get("url", "")),
            images=images,
            attributes=attributes,
        )

    def fetch(self, feed) -> Iterator[RawProduct]:
        cfg = XmlFeedConfig.model_validate(feed.feed_config)
        body = _fetch_capped(cfg.feed_url)
        tree = etree.fromstring(body, parser=_SAFE_XML_PARSER)  # noqa: S320 — hardened parser, see _SAFE_XML_PARSER
        for node in tree.xpath(cfg.product_xpath):
            yield self._build_raw_product(node, cfg)

    def fetch_sample(self, feed, limit: int) -> list[RawProduct]:
        return list(islice(self.fetch(feed), limit))

    def fetch_delta(self, feed) -> Iterator[PriceStockUpdate]:
        cfg = XmlFeedConfig.model_validate(feed.feed_config)
        mapping = cfg.delta_field_mapping or cfg.field_mapping
        body = _fetch_capped(cfg.feed_url)
        tree = etree.fromstring(body, parser=_SAFE_XML_PARSER)  # noqa: S320 — hardened parser, see _SAFE_XML_PARSER
        for node in tree.xpath(cfg.product_xpath):
            external_id = _xpath_text(node, mapping.get("external_id", ""))
            if external_id is None:
                continue
            yield PriceStockUpdate(
                external_id=external_id,
                cost=_to_decimal(_xpath_text(node, mapping.get("cost", ""))),
                currency=_xpath_text(node, mapping.get("currency", "")),
                stock=_to_int(_xpath_text(node, mapping.get("stock", ""))),
            )
