# Connectors

A connector implements `SyncConnector` or `AsyncConnector` (`connectors/base.py`) and registers
under the `atlas_connectors` entry-point group in its own package's `pyproject.toml`. Lifecycle
hooks (`rate_limit_delay`, `batch_size`, `should_retry`, `before_fetch`/`after_fetch`) are no-op by
default — do NOT mark them `@abstractmethod`, or every connector without an override stops
instantiating. The registry validates against **this module's own** `BaseConnector` — a connector
importing a different module's base class silently fails discovery. Concrete vendor connectors live
in their own packages, never in this repo — atlas ships the framework plus the two generic
procurement connectors (`xml_feed`, `scraper`).

Persist is kind-aware, not connector-aware: `import_service._build_or_update_sp` routes
`raw.cost`/`raw.attributes` onto `cost` / `observed_price` / `signals` per `Source.kind`; matched
monitoring/enrichment rows additionally get an `Observation` per run.
