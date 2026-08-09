# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for security.url_guard.safe_get — redirect re-validation + streamed body cap."""

from unittest.mock import MagicMock, patch

import pytest

from django_atlas.security import safe_get


def _mock_response(*, status_code: int = 200, content: bytes = b"", location: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Location": location} if location else {}
    resp.iter_content = MagicMock(return_value=[content])
    resp.raise_for_status = MagicMock()
    return resp


def test_safe_get_returns_body_under_cap():
    with patch("django_atlas.security.url_guard.requests.get", return_value=_mock_response(content=b"hello")):
        assert safe_get("https://example.com/feed", timeout=5, cap=100) == b"hello"


def test_safe_get_follows_safe_redirect():
    hop1 = _mock_response(status_code=302, location="https://example.com/final")
    hop2 = _mock_response(content=b"final-body")
    with patch("django_atlas.security.url_guard.requests.get", side_effect=[hop1, hop2]) as gget:
        body = safe_get("https://example.com/start", timeout=5, cap=100)
    assert body == b"final-body"
    for call in gget.call_args_list:
        assert call.kwargs["allow_redirects"] is False


def test_safe_get_blocks_redirect_to_internal_ip(monkeypatch):
    monkeypatch.setattr("django_atlas.security.url_guard.settings.ATLAS_BLOCK_PRIVATE_HOSTS", True, raising=False)
    hop1 = _mock_response(status_code=302, location="http://169.254.169.254/latest/meta-data/")
    with patch("django_atlas.security.url_guard.requests.get", return_value=hop1):
        with pytest.raises(ValueError, match="Internal host blocked"):
            safe_get("https://example.com/start", timeout=5, cap=100)


def test_safe_get_too_many_redirects_raises():
    hop = _mock_response(status_code=302, location="https://example.com/loop")
    with patch("django_atlas.security.url_guard.requests.get", return_value=hop):
        with pytest.raises(ValueError, match="Too many redirects"):
            safe_get("https://example.com/loop", timeout=5, cap=100)


def test_safe_get_aborts_stream_over_cap():
    resp = _mock_response()
    resp.iter_content = MagicMock(return_value=[b"X" * 60, b"Y" * 60])
    with patch("django_atlas.security.url_guard.requests.get", return_value=resp):
        with pytest.raises(ValueError, match="exceeds cap"):
            safe_get("https://example.com/big", timeout=5, cap=100)
