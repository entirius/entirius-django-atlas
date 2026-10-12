# entirius-django-atlas

A Django module that brings external product data into Entirius, the open-source modular ecommerce platform.
One pipeline serves three kinds of sources: `procurement` (suppliers whose products are pushed to the catalog),
`monitoring` (competitor prices) and `enrichment` (extra product data). Only procurement sources write to the
catalog; the other two are read-only observers.

[![PyPI](https://img.shields.io/pypi/v/entirius-django-atlas.svg)](https://pypi.org/project/entirius-django-atlas/)
[![Python](https://img.shields.io/pypi/pyversions/entirius-django-atlas.svg)](https://pypi.org/project/entirius-django-atlas/)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-blue.svg)](https://github.com/entirius/entirius-django-atlas/blob/develop/LICENSE)

## What it does

- Keeps a registry of sources, their feeds and credentials, with XML feed and scraper connectors.
- Imports source products on a cron schedule and maps attributes and categories through mapping profiles.
- Runs a review and approval workflow, then pushes approved procurement products to the catalog once and
  refreshes cost, quantity and physical attributes on later imports.
- Matches source products to existing catalog products by EAN and selects a primary source per product.
- Records every observation in an append-only log and every field change in an audit log.
- Exposes an admin API (JWT, Pydantic schemas, OpenAPI) for the CMS.

## Installation

```shell
uv add entirius-django-atlas
pip install entirius-django-atlas
```

Optional extras: `entirius-django-atlas[qms]` (stock writes to the warehouse module),
`entirius-django-atlas[lookup]` (registers atlas as a provider of the product lookup module; Python 3.12 or newer).

```python
INSTALLED_APPS = [
    ...
    "django_utils",
    "django_regional",
    "django_pim",
    "django_atlas",
]

# urls.py
urlpatterns = [
    path("", include("django_atlas.urls")),
]
```

Optional settings (`ATLAS_*`: image download, body-size caps, the private-host guard, scraper callback signing)
are defined with their defaults in
https://github.com/entirius/entirius-django-atlas/blob/develop/src/django_atlas/settings.py

## Quick start

```python
from django_atlas.services.observation_service import get_observations

# newest observation per source for one SKU
prices = get_observations("SKU-1", "monitoring", latest_per_source=True)
```

The admin API is mounted under `api/atlas/v2/admin/`.

## Requirements

- Python 3.11 or newer, Django 5.0 or newer.
- PostgreSQL (the observation queries use `DISTINCT ON`).
- A Celery worker consuming the `atlas_default`, `atlas_images` and `atlas_results` queues.
- Entirius packages pulled in: `entirius-django-utils`, `entirius-django-regional`, `entirius-django-pim`.

## Documentation

- Module docs: https://github.com/entirius/entirius-django-atlas/tree/develop/docs
- Entirius documentation: https://docs.entirius.com/
- Changelog: https://github.com/entirius/entirius-django-atlas/blob/develop/CHANGELOG.md

## Part of Entirius

This module runs inside the Entirius backend service (Volkanos) and feeds the product catalog and the pricing engine.

- Backend service: https://github.com/entirius/entirius-service-volkanos
- Docker harness: https://github.com/entirius/entirius-zeno
- Sibling modules: https://github.com/entirius/entirius-django-pim, https://github.com/entirius/entirius-django-pricefighter,
  https://github.com/entirius/entirius-django-lookup

## Development

```shell
make install     # sync dependencies (uv, incl. extras)
make check       # lint + format check (ruff)
make test        # test suite (pytest + pytest-django, needs PostgreSQL via DATABASE_URL)
```

Development and agent instructions: https://github.com/entirius/entirius-django-atlas/blob/develop/AGENTS.md

## License

Mozilla Public License 2.0 (MPL-2.0) — see https://github.com/entirius/entirius-django-atlas/blob/develop/LICENSE.
