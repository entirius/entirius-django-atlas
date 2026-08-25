# AGENTS.md

Generic external-product-data ingestion engine (procurement/monitoring/enrichment) for Volkanos —
distribution `entirius-django-atlas`, Django app `django_atlas`.

## Commands

| Command | Meaning |
|---|---|
| `make install` | sync dependencies (uv, incl. extras) |
| `make check` | lint + format-check (ruff) |
| `make fix` | auto-fix lint + format |
| `make test` | test suite (pytest + pytest-django; postgres via `DATABASE_URL`) |

## Conventions

- English only: code, docs, commits, branches, PRs.
- MPL-2.0: every non-trivial source file carries the license header (pre-commit inserts it).
- Toolchain: uv + ruff + hatchling + pytest; all config in `pyproject.toml`; `uv.lock` committed.
- Git flow: `master` (production) + `develop` (integration); changes land via PR; semver tag on `master`.
- Never rename the package / Django app_label / DB table prefix `django_atlas` — it is a schema contract.
- Migrations are part of the public contract — never edit an already released migration.
- Default: do not commit — git is the user's call.

## Commit Message Format

**NEVER add `Co-Authored-By: Claude ...` (or any other Claude/Anthropic attribution) to commit messages.**

This overrides the default Claude Code behavior of appending a `Co-Authored-By` trailer. Commit messages MUST contain only the user's authored content — no robot footer, no "Generated with Claude Code" line, no co-author trailer.

Same rule applies to PR descriptions: no `Generated with [Claude Code]` footer.

## Architecture

One engine, one data model, discriminated by `Source.kind` (`procurement` / `monitoring` /
`enrichment`) — feed import, review/approval, EAN auto-match, primary-source selection, per-field
audit log, append-only observation log. Only `procurement` sources may push to PIM; `monitoring`
and `enrichment` are read-only observers. Successor to `django-suppliers` (frozen, deprecated).

Hard deps: django-utils (BaseModel), django-regional, django-pim. Soft (optional at runtime):
django-qms (stock writes, try/except), django-pricemanager (consumes `cost_updated_signal` if
installed) — see `pyproject.toml` extras.

Layer rule: `API → Services → Models → DB`. ViewSets do not import models — every ORM access goes
through services. Schemas never import Django models.

```
src/django_atlas/
├── models/          # 12 ORM models, one file per entity (source, source_product, source_feed,
│                    #   mapping profile/attribute/category, source_product_link, import_log,
│                    #   integration_event, source_product_change_log, observation, settings singleton)
├── schemas/         # Pydantic: contract.py (RawProduct, PriceStockUpdate), requests/, responses/
├── services/        # framework-agnostic business logic; kind_guard.py = single source of truth
│                    #   for PUSH_BLOCKED_KINDS; *_writer.py = PIM/QMS/pricemanager write boundaries
├── connectors/      # base (Sync/Async + lifecycle hooks), xml_feed, scraper
├── signals/         # definitions, handlers, killswitch
├── tasks/           # Celery: feed execution/dispatch, push pipeline, image download, retentions
├── api/admin/       # views (incl. supplier/competitor facades), permissions, pagination, throttling
└── management/commands/
```

### Signals

Receivers MUST be idempotent (Celery retries, delta re-runs, force re-push).

| Signal | Emitted by | Subscribers |
|---|---|---|
| `source_products_imported_signal` | `import_service.execute_feed` after successful sync | auto-push pipeline (gated by `SourceSettings.auto_push_enabled` + `kind_guard`, in `transaction.on_commit`) |
| `cost_updated_signal` | `pricemanager_writer.log_cost` (procurement only) | `django_pricemanager` (soft dep) |
| `source_product_pushed_signal` | `push_service` on push status transitions | image-dispatch task (`atlas_images` queue) |
| `primary_switched_signal` | `primary_strategy_service.apply_primary_switch` | none in tree yet (reserved) |
| `observation_recorded_signal` | `observation_service.record_observation` | none in tree yet (reserved) |

Killswitches: `signals.killswitch.suppress_source_signals()` (thread-local) and
`SourceSettings.auto_push_enabled` (DB, cached 60s). Push is also gated per-kind by
`services.kind_guard` regardless of killswitch state.

### Kind-aware gates (defense-in-depth)

Only `procurement` may write to PIM — enforced at three independent points: (1)
`push_service.preflight_check` (single choke point for every push path), (2)
`pim_writer.init_push_to_channel` / `force_repush_to_channel` re-assert
`kind_guard.assert_push_allowed()`, (3) `pricemanager_writer.log_cost` early-returns for
non-procurement. Manual links to EXISTING RealProducts stay allowed for non-procurement sources.

### Push target resolution

`pim_writer._resolve_push_target` decides which RealProduct a push writes to, and an existing
`SourceProduct.real_product` outranks every other rule: whatever attached the SP (lookup UI,
enrichment proposal, an earlier push) already chose the target, so the generated SKU and the EAN
auto-match are both skipped and a `pushed_onto_linked_realproduct` event (`matched_via:
existing_link`) records why — kept distinct from `auto_linked_to_existing_realproduct` (the EAN
auto-match below) so a dashboard counting one never double-counts the other. Only an unlinked SP
walks the legacy path — EAN auto-match first, `generate_sku` + `get_or_create` after.
`force_repush_to_channel` mirrors it; without that, a lookup-linked SP would push onto a freshly
generated SKU and spawn the duplicate the link exists to prevent.

### Observation log

Append-only — no update/delete path in services. `sku` is a plain CharField (never FK).
`observation_service.record_observation` is the single write choke point. Read API:
`get_observations(sku, kind, latest_per_source=True)` (empty → `[]`),
`get_skus_with_valid_observations(kind, max_age)` (single query),
`get_observations_bulk(skus, kind)` (Postgres `DISTINCT ON`; skus with no observations are absent
from the dict — batch "nothing found" contract is omission, single-sku contract is `[]`).

### API surface

URL prefix `/api/atlas/v2/admin/`; JWT + `IsAdminUser`; OpenAPI via drf-spectacular. Resources:
sources (+ data-keys/data-values/credentials/delete-impact), supplier & competitor facades
(kind-forced projections of Source — `kind` never accepted from the client), feeds (+ trigger/test),
mapping profiles (+ validate), attribute/category mappings, products (+ bulk and per-pk review/push
actions), product-links (+ set-primary), observations (read-only), import-logs, events
(+ acknowledge), PIM SKU bridge (`pim-sku/...` — `<path:sku>` routes MUST stay after literal-prefix
routes in `urls.py`), primary override, realproducts (merge-by-ean/auto-matched/duplicates), push,
settings singleton, connectors discovery.

### Connectors

Registered through the `atlas_connectors` entry-point group (`xml_feed`, `scraper`).
Protocol, settings and adding a connector: `docs/connectors.md`.

### Lookup provider / enrichment adapter

Atlas registers itself into django-lookup as the `atlas_source_product` provider and exposes
the `duplicate_in_pim` enrichment adapter. **django-lookup is a soft dependency**: every
`django_lookup` import in this package is function-local, so atlas runs without it (covered by
`test_import_service` / `test_link_to_realproduct`). Declared as the optional `lookup` extra.
Contract, payload shapes and the undo path: `docs/lookup-integration.md`.

### Scheduling

`SourceFeed.schedule_cron` (5-field cron) is consumed by the `django_atlas.dispatch_scheduled_feeds`
beat task (host service registers it, runs every minute). It matches the current UTC minute against
parsed cron fields — NOT `is_due(last_run_at)` (a long-running feed would be re-enqueued every tick).
Respects `SourceSettings.feed_scheduling_enabled` and per-feed `is_active`; invalid cron strings are
skipped with a warning.

## Testing

Postgres required (`DATABASE_URL`, default `postgresql://postgres:postgres@localhost:5432/test`).
INSTALLED_APPS in tests: `django_regional`, `django_pim`, `django_atlas` — NOT `django_qms` /
`django_pricemanager` (soft / out-of-scope). Fixture URLs use `example.com`;
`ATLAS_BLOCK_PRIVATE_HOSTS = False` in test settings — security tests opt back in via monkeypatch.

## Reference Docs

| File | Content |
|------|---------|
| `docs/lookup-integration.md` | Lookup provider contract + `duplicate_in_pim` enrichment adapter |
| `docs/connectors.md` | Connector entry-point protocol and settings |
| `docs/audit-log.md` | Audit / change-log model and retention |
| `docs/auto-ean-match.md` | Automatic EAN matching rules |
| `docs/mapping-modifiers.md` | Attribute/category mapping modifiers |
| `docs/erd-config.yaml` | ERD diagram config |
