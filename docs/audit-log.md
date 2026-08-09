# Audit log — per-field SourceProduct change tracking

`SourceProductChangeLog` is a per-field audit trail for every mutation that
touches a `SourceProduct` after it has been pushed to PIM. It answers the
operator question that `IntegrationEvent` can't: **"What exactly changed on this
SKU, when, by whom, and did it reach PIM?"**

The model is observability — best-effort and out-of-band. Audit log writes never
block the sync, push, or operator-edit flow; a failed log call surfaces as a
WARNING and the data path completes normally.

## Write paths (6 hooks)

| Where | `source` | When | `applied_to_pim` |
|---|---|---|---|
| `process_full_sync._build_or_update_sp` | `full_sync` | Per-key diff of SP.data + scalar fields (name/cost/currency/stock/ean) + image_urls. **Only for SPs in PUSHED_STATUSES** — pre-push staging churn is noise. | `False` |
| `process_delta_sync` cost/currency/stock apply | `delta_sync` | Scalar diff (cost / currency / stock). **Only for pushed SPs.** | `False` (D5 cost defer) |
| `_apply_physical_update_to_real_product` | `delta_sync` | Per-field diff of physical attrs (weight/ean/dims) on RealProduct. | `True` |
| `pim_writer.init_push_to_channel` | `init_push` | Single baseline snapshot of SP state at first push to each channel. | `True` |
| `pim_writer.force_repush_to_channel` | `force_repush` | Per-attribute diff + categories diff + physical RealProduct diff + picture-reset marker. | `True` |
| `product_service.update_sp` | `operator_sp_edit` | Per-field diff of operator-edited SP fields. | `False` |

Additional `source` values are reserved schema-first for upcoming stages:
`auto_link`, `auto_primary_switch`, `manual_override`, `emergency_switch`. The migration
is stable — these stages add emit calls only, no enum churn.

## Indices

```python
class Meta:
    indexes = [
        models.Index(fields=["source_product", "-created_at"]),  # per-SP timeline
        models.Index(fields=["real_product_sku", "applied_to_pim", "-created_at"]),  # PIM panel bulk lookup
        models.Index(fields=["source", "-created_at"]),  # analytics filter
    ]
```

The `real_product_sku` field is denormalized from the linked `RealProduct.sku` at
write time so PIM-side queries don't need a join through `SourceProduct` to find
"unseen changes for SKU X".

## Retention

Default 90 days. Configured via `SourceSettings.change_log_retention_days`
(mirrors the `integration_event_retention_days` pattern). Unlike `IntegrationEvent`,
the audit log has no critical-severity bypass — every old row is deleted.

### Celery beat schedule (service side)

The task is registered as `django_atlas.prune_source_change_logs` on the
`atlas_default` queue. Service `celery.py` should add:

```python
app.conf.beat_schedule = {
    ...
    "prune-source-change-logs": {
        "task": "django_atlas.prune_source_change_logs",
        "schedule": crontab(hour=4, minute=0),  # daily 04:00 — 1h after primary-source eval
    },
}
```

### Manual prune

```bash
python manage.py prune_source_change_logs              # uses settings retention
python manage.py prune_source_change_logs --days 0     # delete everything (testing only)
python manage.py prune_source_change_logs --days 30    # one-off shorter window
```

## Field-path conventions

| `field_path` | What it represents |
|---|---|
| `name`, `cost`, `currency`, `stock`, `ean` | Scalar SP fields |
| `image_urls` | Full before/after list (order-sensitive) |
| `data.{key}` | Top-level key in `SourceProduct.data` JSONB |
| `physical.{weight,ean,width,height,deep}` | RealProduct physical attribute |
| `snapshot.{channel_idx}` | INIT push baseline (single entry per channel) |
| `attribute.{feature_idx}` | force_repush PIM ProductAttribute replacement |
| `categories.{channel_idx}` | force_repush category list rewrite |
| `pictures.{channel_idx}` | force_repush picture reset marker (count only; async repopulate) |
| `feature_set_idx_override` | Operator edit on SP |

## Performance budget

- Full sync of ~3859 SPs × ~5 changed keys ≈ ~20k inserts per run.
  Mitigation: `bulk_create(batch_size=500)` via `audit_service.log_changes_bulk`.
  Workbook target: <2s for the audit write portion.
- Index 2 (`real_product_sku, applied_to_pim, -created_at`) is the hot path for
  the upcoming PIM panel "Show me unseen changes for SKU X" query.

Verify on a real seed with `EXPLAIN ANALYZE`:

```sql
EXPLAIN ANALYZE
SELECT id, source, field_path, before, after, created_at
FROM django_atlas_sourceproductchangelog
WHERE real_product_sku = 'AC-abc123'
  AND applied_to_pim = false
ORDER BY created_at DESC
LIMIT 50;
```

The plan MUST use `Index Scan using supp_changelog_sku_unseen_idx`. Seq Scan
means the index didn't get picked — re-`ANALYZE` the table or check stats.

## Out-of-band semantics

Every write path wraps the audit call:

```python
try:
    audit_service.log_changes_bulk(drafts)
except Exception:  # noqa: BLE001 — audit must be best-effort
    logger.warning("audit_service.log_changes_bulk failed in %s", context, exc_info=True)
```

A failing audit table (full disk, migration drift, transient connection) MUST NOT
abort an in-flight full sync, push, or operator edit. The cost is silent
observability loss for the duration of the outage — visible as `WARNING
audit_service.log_changes_bulk failed` in the `django_atlas` logger.

## Out of scope

- Read API (`GET /pim-sku/{sku}/changes/`, `has-changes/` bulk)
- CMS badges + PIM "Source" tab
- Sources panel "Updated" badge + 4th review mode
- Cross-source auto-link / auto-primary event emission
