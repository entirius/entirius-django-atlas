# Auto EAN-match

When a source feed delivers a `SourceProduct` whose EAN matches an existing
`RealProduct` in PIM, the push pipeline now attaches the SP to that existing
RealProduct via `SourceProductLink` instead of minting a per-source-prefixed
SKU. That's how `multi_source_overlap` finally happens in the natural flow.

Step 2 — auto-primary selection with
hysteresis / cooldown / cron / emergency switch — ships separately.

## Flow

```
init_push_to_channel(sp, profile, channel_idx, user)
    │
    ▼
realproduct_match_service.find_match_by_ean(sp, source)
    │
    ├── source.disable_ean_auto_link?         → None  → standard flow
    │
    ├── sp.ean blank?                            → None  → standard flow
    │
    └── RealProduct.filter(ean=sp.ean)            → existing_rp or None
        .order_by("id").first()
                │
                ├── None → standard flow:
                │         RealProduct.get_or_create(sku=generate_sku(...), defaults=rp_defaults)
                │         _detect_multi_source_overlap(sp, source, sku)
                │
                └── existing_rp → physical_tolerance_check(rp_defaults, existing_rp, source)
                                        │
                                        ├── passed → AUTO-LINK:
                                        │            real_product = existing_rp
                                        │            _emit_auto_link_event(info)
                                        │            _log_auto_link_audit(source=auto_link)
                                        │
                                        └── failed → FALLBACK:
                                                    _emit_tolerance_violation_event(warning)
                                                    RealProduct.get_or_create(sku=generate_sku(...))
```

After the decision the standard pipeline continues unchanged: `Product.get_or_create`,
attribute / category mappings, `_persist_sp_after_push`, `_write_qms_cost_link`,
`_emit_pushed_signal`, audit baseline. The only call site that changed downstream
is `_write_qms_cost_link` — it now passes `set_primary_if_first=True` to
`product_link_service.upsert_for_push` so the first link for a SKU becomes the
primary one.

## Tolerance check

Compared physical fields: **`weight`, `width`, `height`, `deep`**.

`new_value` comes from `_real_product_defaults(sp, profile)` — the same defaults
dict that would be used to seed a new RealProduct. `existing_value` comes from
the candidate `RealProduct` columns. Per-field diff:

```
diff_pct = abs(new - existing) / max(abs(new), abs(existing), 0.001) * 100
field fails if diff_pct > Source.realproduct_match_tolerance_pct (default 10)
```

The denominator floor (0.001) keeps 0-vs-near-0 comparisons stable.

| Source knob | Default | Effect |
|---|---|---|
| `realproduct_match_tolerance_pct` | 10 | Max acceptable per-field diff (%). |
| `realproduct_match_strict` | False | When True, missing field on either side = fail. |
| `disable_ean_auto_link` | False | When True, skip lookup entirely (always new RP). |

Non-strict (default) skips comparison for fields missing on either side. Pass requires
**zero failed fields**. Sample comparable seed (Foliopak 350×450, EAN `5906214804074`):

| Side | weight | width | height | deep |
|---|---|---|---|---|
| existing RP (Acme) | `0.150` | `20.00` | `10.00` | `5.00` |
| SP (Globex) | `0.155` (3.3%) | `20.50` (2.4%) | `10.10` (1.0%) | `5.05` (1.0%) |

All four fields under 10% → **auto-link**. SourceProductLink for Globex is
created with `is_primary=False`; existing Acme link stays primary. Auto-primary
re-evaluation belongs to auto-primary selection.

## Audit + events

| Outcome | IntegrationEvent | SourceProductChangeLog |
|---|---|---|
| Auto-link | `auto_linked_to_existing_realproduct` (info) | `source=auto_link`, `field_path=real_product.link`, after={sku, ean, source_idx, diffs_pct, skipped_fields} |
| Tolerance fail (fallback) | `physical_tolerance_violation` (warning) | (no audit — no link was made) |
| Manual unlink | `manual_unlink_from_realproduct` (info) | `source=manual_unlink`, `field_path=real_product.unlink`, before={sku: old}, after={sku: new, ean} |

Auto-link event details ship `existing_source_idxs` so the operator can see
who else owns the RealProduct in one panel hop.

## Manual unlink — operator escape hatch

```
POST /api/atlas/v2/admin/products/{pk}/unlink-from-realproduct/
```

Creates a fresh RealProduct (per-source-prefixed SKU via `generate_sku`),
moves the SP, deletes the old link, creates a new one with `is_primary=True`.
Original RealProduct stays — other sources' links may still reference it.

Use when the auto-link decision is wrong (e.g. shared EAN actually points to
different physical products that slipped under the tolerance threshold).

400 — SP not linked, or generated SKU collides with an existing RealProduct.
404 — SP missing.

## Duplicate triage — `find_duplicate_realproducts`

```
docker compose exec volkanos python manage.py find_duplicate_realproducts --by ean
```

Read-only. Reports each EAN with multiple RealProducts plus a MERGE/REVIEW
suggestion based on the max pairwise weight diff:

```
Found 1 EAN group(s) with multiple RealProduct:

  EAN 5906214804074 (2 RealProducts):
    • AC-ce5b9e3089ff weight=0.150 width=20.00 height=10.00 deep=5.00 sources=acme ★
    • GX-8a1beaee63fe weight=0.155 width=20.50 height=10.10 deep=5.05 sources=globex
    Suggestion: MERGE (max weight diff 3.3% within 10% tolerance)
```

Operator decides per group:

- **MERGE** — use the unlink endpoint backwards.
- **REVIEW** — physical fields diverge too much, probably different products.
- **KEEP SEPARATE** — currently a manual decision; a `RealProduct.verified_separate`
  flag is a candidate for a future polish.

## Edge cases

| Case | Behaviour |
|---|---|
| Empty / null SP EAN | No lookup. Standard `get_or_create(sku=generate_sku(...))`. |
| RealProduct.ean blank, SP has EAN | No match (the lookup filters on `ean=sp.ean` literally). Auto-link does NOT backfill the EAN onto the existing RP. |
| Multiple RealProducts share the EAN | `.order_by("id").first()` picks the oldest. Operator can disambiguate via the management command. |
| Tolerance fail | Always falls back to new RealProduct; never raises, never blocks the push. |
| `disable_ean_auto_link=True` | Skip lookup entirely. Per-source opt-out for "noisy data" sources. |
| Auto-link wrong → operator unlinks | Endpoint moves SP to fresh RealProduct; original RP untouched. |
| 2+ sources identical cost+stock | Out of scope here — auto-primary handles it (deterministic by cost ASC, source.id ASC). |
| EAN-rename on source  | Out of scope here — `RealProduct.ean` is not modified by auto-link. |

## First-link-primary semantics

`product_link_service.upsert_for_push(sku, source, external_id, set_primary_if_first=True)`
sets `is_primary=True` when (and only when) the link is freshly created AND no
other link exists for this SKU. The operator-flags contract is preserved on UPDATE — we never overwrite
operator state. The flip on CREATE was necessary so the cost subscriber
stops ignoring single-source links that previously defaulted to `is_primary=False`.

Auto-linked SPs (second source on an existing RP) get `is_primary=False` at
creation — the auto-primary strategy is what flips it.

## Related code

- `services/realproduct_match_service.py` — `find_match_by_ean`, `physical_tolerance_check`, `ToleranceResult`.
- `services/pim_writer.py:_emit_auto_link_event` / `_emit_tolerance_violation_event` / `_log_auto_link_audit` — event + audit emit helpers.
- `services/product_link_service.py:unlink_sp_from_realproduct` — operator force-unlink service.
- `api/admin/views/product_views.py:unlink_from_realproduct` — endpoint wrapper.
- `management/commands/find_duplicate_realproducts.py` — duplicate triage CLI.
- `models/source.py` — `realproduct_match_tolerance_pct`, `realproduct_match_strict`, `disable_ean_auto_link`.
- `migrations/0013_etap_13a_ean_match.py` — schema migration.

## Related docs

- `docs/audit-log.md` — full ChangeLogSource enum + write paths.
- `docs/audit-log.md` § events — `multi_source_overlap` event spec (now actually fires).
