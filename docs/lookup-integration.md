# Lookup & enrichment integration

# Lookup provider

`services/lookup_provider.py` is atlas's read boundary for the **django-lookup** module. Lookup loads
it lazily from `LOOKUP_PROVIDERS = {"atlas_source_product": "django_atlas.services.lookup_provider"}`
and calls duck-typed module-level functions (`iter_items`, `get_item`, `basic`, `detail_url`,
`signal_specs`) — nothing from django-lookup is imported here, `ProviderItem` / `BasicData` are
mirrored so an optional consumer never becomes an atlas dependency.

Items are the **candidate pool**: `real_product IS NULL` and status not `rejected`. `ref` =
`<source.idx>:<external_id>` (stable across re-imports); `detail_url` resolves the pk, the admin API
addressing source products by pk only. brand / mpn / physicals come from `data` through the source's
active `SourceAttributeMapping` rows (modifier applied) and fall back to conventional keys
(`brand` / `manufacturer`, `mpn`, `weight`, …). Images stay remote URLs — never fetched here. The
single signal spec re-runs the provider on every row-at-a-time `SourceProduct.save()` (link/unlink,
manual edit); linking one drops it from the pool, which is how lookup deletes its fingerprint row.

Freshness has a second path: `import_service` persists full-sync batches with
`bulk_create`/`bulk_update`, which fire no `post_save` — the signal above never sees them.
`import_service._enqueue_lookup_refresh` closes that gap, enqueueing `django_lookup.tasks.
refresh_fingerprints(kind, refs)` — the batched twin of the single-ref task, a thin loop over the
same idempotent logic — in chunks of `django_lookup.constants.REFRESH_TASK_BATCH` (200) right after
each batch's transaction commits (never inside it — see `_run_batch_with_retry`'s
no-side-effects-in-a-retryable-attempt rule). One publish per up-to-200 changed refs, not one per
row: a first full sync of a large feed changes every row, and a per-row publish would flood the
`lookup` queue shared with image embedding. `_lookup_ref(source, external_id)` delegates to
`lookup_provider.ref_for` (never re-implements the `<source.idx>:<external_id>` format) via a
transient, unsaved `SourceProduct` — no query, and it covers a ref with no live row too (see rename
below). Soft dependency, same posture as `django_lookup.signals._enqueue`: import guarded
(`ImportError`/`RuntimeError` — the latter is Django's own "on PYTHONPATH but not in INSTALLED_APPS"
signature, `qms_writer` hits the same one), broker failures swallowed and logged, recoverable with
`lookup_reconcile`.

Three writes need this path, and all three are covered:
  - **new/updated rows** (`_process_full_sync_batch`) — enqueued via `_lookup_ref(source,
    sp.external_id)` once the batch's `bulk_create`/`bulk_update` commits.
  - **delisting** (`process_full_sync`'s non-pushed stale-row bulk `.update()` to `REJECTED`) —
    another bulk write that fires no signal either. The refs are collected from `stale_non_pushed.
    values_list("external_id", ...)` BEFORE the `.update()` runs, and enqueued right after: `REJECTED`
    is excluded from `lookup_provider.candidates()`, so refreshing these refs is how the now-stale
    fingerprint rows get deleted — without it a delisted row stays proposable for a product that
    vanished from the feed. Pushed stale rows are untouched here (status doesn't change, only an
    IntegrationEvent fires) so they need no refresh.
  - **EAN-matched rename** (`_build_or_update_sp` returning a non-None `rename_from`) — the row's
    `external_id` is overwritten in place, so its old ref has no row left to build from and nothing
    else will ever refresh it again under that ref. `_process_full_sync_batch` enqueues both:
    `_lookup_ref(source, sp.external_id)` (post-rename, the live row) and `_lookup_ref(source,
    rename_from)` (pre-rename, orphaned — refreshing a ref the provider no longer serves under that
    ref is exactly how django-lookup deletes a stale row, same mechanism as delisting above).

`process_feed_results` (the scraper-callback path) and `update_or_create_source_product` (the
single-item test helper) do not enqueue at all — narrower, signal-free paths outside this checkpoint's
scope; a future consumer of either should wire them through `_enqueue_lookup_refresh` too before
relying on lookup freshness there.

# Enrichment adapter (duplicate_in_pim)

`services/enrichment_adapter.py` is atlas's write boundary for the **django-enrichment** bus, loaded
lazily from `ENRICHMENT_ADAPTERS = {"atlas": "django_atlas.services.enrichment_adapter"}`. One check
(`duplicate_in_pim`) and one target kind (`link_to_realproduct`): `find_gaps` runs `lookup.check` per
unlinked source product and proposes the best `match`/`review` candidate, `apply` sets
`SourceProduct.real_product` (+ a `linked_via_lookup_proposal` event), `revert` clears it only while
the link is still the one it wrote, and the optional `on_reject` hook records the operator's "no" in
django-lookup's `DedupDecision` log so the pair is never proposed again. `subject_ref` is the lookup
provider's ref (`<source.idx>:<external_id>`).

v1 is **always a proposal** (decision #3) — nothing is auto-linked and the EAN auto-link in
`pim_writer` is untouched. `Source.auto_accept_min_score` is the documented seam for a later version
and is read by no code path. django-lookup stays a **soft** dependency: every import of it sits
inside a function, and the adapter tests fake those four touchpoints (the test suite does not install
django-lookup). Fixture: `fixtures/enrichment_spawn_rule_duplicate_in_pim.json` (the SpawnRule the
operator runs).
