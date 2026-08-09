# entirius-django-atlas

Generic external-product-data ingestion engine for the Volkanos ecommerce platform, discriminated
by `kind` (procurement / monitoring / enrichment).

Manages the source registry, feed-driven product import pipeline, review/approval workflow, and
one-time push to PIM with cyclic delta refresh of cost / qty / physical attributes. Only
`procurement` sources may push to PIM — `monitoring` and `enrichment` sources are read-only
observers.

## Install (development)

```bash
uv sync --all-extras
```

## Test

```bash
# Postgres required; defaults to postgresql://postgres:postgres@localhost:5432/test
DATABASE_URL=postgresql://... uv run pytest -x -q
```

## Documentation

- Module reference: `volkanos/modules/atlas/` in [docs.entirius.com](https://docs.entirius.com)
- Module layout + dev patterns: [AGENTS.md](AGENTS.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
