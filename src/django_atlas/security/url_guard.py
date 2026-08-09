# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SSRF guard for external URL fetches (feeds + image downloads).

Blocks non-http(s) schemes, missing/un-resolvable hosts, and (when enabled)
internal IP ranges (RFC1918, loopback, link-local, reserved, multicast).

Set ``ATLAS_BLOCK_PRIVATE_HOSTS = False`` in Django settings to disable
the IP check (useful in tests; default True).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


def _is_internal_ip(host: str) -> bool:
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host: {host}") from exc
    for _family, *_rest, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def assert_safe_url(url: str) -> None:
    """Raise ``ValueError`` if URL is unsafe for outbound fetch.

    Blocks: non-http(s), missing host, private/loopback/link-local IPs.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL missing hostname")
    if getattr(settings, "ATLAS_BLOCK_PRIVATE_HOSTS", True) and _is_internal_ip(parsed.hostname):
        raise ValueError(f"Internal host blocked: {parsed.hostname}")


def safe_get(url: str, *, timeout: float, cap: int, headers: dict[str, str] | None = None) -> bytes:
    """SSRF-safe GET: re-validates the host on every redirect hop, streams up to `cap` bytes.

    Redirects are never auto-followed (``allow_redirects=True`` would connect to each hop's
    host before ``assert_safe_url`` ever sees it) — every ``Location`` is validated before
    being followed, and the body is read in chunks so an oversized response is aborted mid-
    stream instead of fully materialized first.
    """
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        assert_safe_url(current_url)
        response = requests.get(current_url, timeout=timeout, headers=headers, stream=True, allow_redirects=False)
        if response.status_code in _REDIRECT_STATUS_CODES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Redirect response missing Location header")
            current_url = urljoin(current_url, location)
            continue
        try:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > cap:
                    raise ValueError(f"Response body exceeds cap of {cap} bytes")
            return bytes(body)
        finally:
            response.close()
    raise ValueError(f"Too many redirects (> {_MAX_REDIRECTS}) fetching {url}")
