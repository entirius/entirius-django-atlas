# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Kind gate — only `procurement` sources may write to PIM.

`monitoring` and `enrichment` sources are read-only observers (competitor price
watching / signal ingestion): their SourceProducts exist, receive delta cost/stock
updates and change-log history, but MUST NEVER write to PIM/QMS/PriceManager and
never participate in primary-source selection. Manual links to EXISTING RealProducts
stay allowed — that is how future price alerts / enrichment signals attach to the
catalog.
"""

from django_atlas.enums import SourceKind
from django_atlas.models import Source

PUSH_BLOCKED_KINDS = frozenset({SourceKind.MONITORING.value, SourceKind.ENRICHMENT.value})
PUSH_BLOCKED = "kind_push_blocked"


def is_push_blocked(source: Source) -> bool:
    return source.kind in PUSH_BLOCKED_KINDS


def assert_push_allowed(source: Source, action: str) -> None:
    """Raise ValueError when a non-procurement source attempts a PIM-bound action."""
    if is_push_blocked(source):
        raise ValueError(f"Source '{source.idx}' has kind '{source.kind}' — {action} is not allowed")


__all__ = ["PUSH_BLOCKED", "PUSH_BLOCKED_KINDS", "assert_push_allowed", "is_push_blocked"]
