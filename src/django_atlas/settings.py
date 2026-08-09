# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Module-level settings and env-var glue for django-atlas.

Module settings live in the SourceSettings singleton (see models/source_settings.py).
This file is reserved for env-var-derived constants and Django settings overrides
that future stages of the implementation plan require.
"""

from django.conf import settings

ATLAS_IMAGE_DOWNLOAD_TIMEOUT_S = getattr(settings, "ATLAS_IMAGE_DOWNLOAD_TIMEOUT_S", 30)
ATLAS_IMAGE_DOWNLOAD_RETRIES = getattr(settings, "ATLAS_IMAGE_DOWNLOAD_RETRIES", 2)
ATLAS_IMAGE_DOWNLOAD_USER_AGENT = getattr(settings, "ATLAS_IMAGE_DOWNLOAD_USER_AGENT", "Volkanos-Atlas/1.0")
ATLAS_BULK_BATCH_SIZE = getattr(settings, "ATLAS_BULK_BATCH_SIZE", 500)

# Body-size caps for outbound HTTP (defense in depth — source feeds + image hosts are untrusted).
ATLAS_MAX_FEED_BYTES = getattr(settings, "ATLAS_MAX_FEED_BYTES", 200 * 1024 * 1024)  # 200 MB
ATLAS_MAX_IMAGE_BYTES = getattr(settings, "ATLAS_MAX_IMAGE_BYTES", 50 * 1024 * 1024)  # 50 MB

# SSRF guard — block internal IP ranges (RFC1918, loopback, link-local, reserved).
# Disabled in tests via monkeypatch; production keeps True.
ATLAS_BLOCK_PRIVATE_HOSTS = getattr(settings, "ATLAS_BLOCK_PRIVATE_HOSTS", True)

QUEUE_DEFAULT = "atlas_default"
QUEUE_IMAGES = "atlas_images"
QUEUE_RESULTS = "atlas_results"

# Phase-2 HMAC signing of scraper-workers callback payloads.
# When set, every `process_scraper_results_task` call MUST include a `signature` kwarg
# matching `hmac_sha256(secret, run_id_bytes + canonical_json(payload))`.
# When unset (default), signature is not enforced — single-tenant deploys can keep this
# off; multi-tenant / shared RabbitMQ MUST set it.
ATLAS_SCRAPER_CALLBACK_HMAC_SECRET = getattr(settings, "ATLAS_SCRAPER_CALLBACK_HMAC_SECRET", "")
