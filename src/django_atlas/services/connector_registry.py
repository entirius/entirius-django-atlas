# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from importlib import metadata

from django_atlas.connectors.base import BaseConnector

_ENTRY_POINTS_GROUP = "atlas_connectors"
_REGISTRY: dict[str, type[BaseConnector]] = {}


def discover_connectors() -> dict[str, type[BaseConnector]]:
    if _REGISTRY:
        return _REGISTRY
    for entry_point in metadata.entry_points(group=_ENTRY_POINTS_GROUP):
        connector_cls = entry_point.load()
        if not isinstance(connector_cls, type) or not issubclass(connector_cls, BaseConnector):
            raise TypeError(f"Entry point '{entry_point.name}' did not resolve to BaseConnector subclass")
        _REGISTRY[connector_cls.connector_kind] = connector_cls
    return _REGISTRY


def reset_registry_for_tests() -> None:
    """Test helper — clear cache so fresh discovery runs after monkeypatch."""
    _REGISTRY.clear()


def get_connector(connector_kind: str) -> BaseConnector:
    registry = discover_connectors()
    try:
        return registry[connector_kind]()
    except KeyError as exc:
        raise KeyError(f"connector_kind '{connector_kind}' is not registered") from exc


def list_connectors() -> list[dict]:
    registry = discover_connectors()
    out: list[dict] = []
    for kind, cls in registry.items():
        out.append({"kind": kind, "is_async": cls.is_async, "config_schema": cls.config_schema().model_json_schema()})
    return out


def validate_feed_config(connector_kind: str, config: dict) -> None:
    connector = get_connector(connector_kind)
    connector.validate_config(config)
