# Changelog

All notable changes to entirius-django-atlas are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-08-09

Initial public release.

- Source registry discriminated by `kind` (`procurement` / `monitoring` / `enrichment`) — one
  engine, one data model shared across all three kinds. Successor to `django-suppliers`
  (frozen, deprecated).
- Feed-driven import pipeline (full + delta sync), review/approval workflow, EAN auto-match,
  primary-source selection, per-field audit log, append-only observation log.
- Kind-aware push gates: only `procurement` sources may write to PIM (enforced at
  `push_service.preflight_check`, `pim_writer`, and `pricemanager_writer` independently).
- Pluggable connector framework (`atlas_connectors` entry-point group) with `xml_feed` and
  `scraper` connectors; cron-scheduled feed dispatch.
- Admin API v2 (`/api/atlas/v2/admin/`) with supplier/competitor facades, OpenAPI via
  drf-spectacular.
- Soft integrations: `django_qms` (stock writes) and `django_pricemanager`
  (`cost_updated_signal`) — both optional at runtime.
