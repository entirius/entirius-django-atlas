# Testing

Postgres required (`DATABASE_URL`, default `postgresql://postgres:postgres@localhost:5432/test`).
INSTALLED_APPS in tests: `django_regional`, `django_pim`, `django_atlas` — NOT `django_qms` /
`django_pricemanager` (soft / out-of-scope). Fixture URLs use `example.com`;
`ATLAS_BLOCK_PRIVATE_HOSTS = False` in test settings — security tests opt back in via monkeypatch.

```bash
make test    # pytest + pytest-django; PostgreSQL via DATABASE_URL
```
