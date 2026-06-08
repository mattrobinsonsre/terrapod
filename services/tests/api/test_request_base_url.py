"""Tests for tfe_v2._request_base_url host/scheme validation (Semgrep
#174/#175).

The function takes operator-controlled headers (X-Forwarded-Host,
Host, X-Forwarded-Proto) and splices them into URL prefixes that go
into JSON:API response bodies (and downstream into redirect
Location: headers). A malicious upstream proxy injecting CRLF or
other separators into those headers could end up in user-visible
URLs without the strict validation.

We test the validators themselves directly — that's the security-
relevant surface — plus a couple of integration-shape cases through
the full function.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from terrapod.api.routers.tfe_v2 import (
    _is_safe_host,
    _is_safe_scheme,
    _request_base_url,
)


class TestIsSafeHost:
    @pytest.mark.parametrize(
        "host",
        [
            "terrapod.local",
            "terrapod.example.com",
            "api.terrapod.example.com",
            "host-with-dashes.example.com",
            "h1.h2.h3.h4.h5.example.com",
            "terrapod.local:8443",
            "host.example.com:443",
            "10-0-0-1.example.com",  # numeric segments are fine in DNS
            "x.y",  # minimum sensible host
        ],
    )
    def test_accepts_safe_hosts(self, host: str) -> None:
        assert _is_safe_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            # CRLF injection — the attack the validation is here for.
            "evil.com\r\nLocation: https://attacker/",
            "evil.com\rfoo",
            "evil.com\nfoo",
            # Other separator characters that could split headers or URLs.
            "evil.com\tfoo",
            "evil.com foo",
            "evil.com/path",  # slashes never legal in a Host header
            "evil.com?q=1",
            "evil.com#frag",
            "evil.com@attacker.com",  # userinfo smuggling
            # Empty / falsy.
            "",
            # Just-port no host.
            ":8443",
            # Port that doesn't fit \d{1,5}.
            "host.example.com:123456",
            "host.example.com:abc",
            # Length explosion — RFC 1123 caps at 253; we cap host portion
            # at 253 chars (port may follow).
            "a" * 254,
            # IPv6 literal (brackets aren't in our whitelist; if we ever
            # need IPv6 callback hosts that's an explicit addition, not
            # a regression to allow [] through).
            "[::1]",
            "[2001:db8::1]:8443",
        ],
    )
    def test_rejects_unsafe_hosts(self, host: str) -> None:
        assert _is_safe_host(host) is False


class TestIsSafeScheme:
    def test_accepts_http_and_https(self) -> None:
        assert _is_safe_scheme("http") is True
        assert _is_safe_scheme("https") is True

    @pytest.mark.parametrize(
        "scheme", ["", "HTTP", "HTTPS", "javascript", "data", "file", "https:"]
    )
    def test_rejects_anything_else(self, scheme: str) -> None:
        assert _is_safe_scheme(scheme) is False


def _request_with(headers: dict[str, str], url_scheme: str = "https") -> MagicMock:
    """Build a minimal Request-shaped mock with the headers and scheme
    `_request_base_url` actually reads."""
    req = MagicMock()
    req.headers = headers
    req.url = MagicMock()
    req.url.scheme = url_scheme
    return req


class TestRequestBaseUrl:
    """Integration shape: full _request_base_url through the validator
    branches. callback_base_url is mocked to a known sentinel so we
    can prove the fallback fires when a header is rejected."""

    _FALLBACK = "https://callback.example.com"

    def _patched_settings(self):
        mock_settings = MagicMock()
        mock_settings.auth.callback_base_url = self._FALLBACK
        return patch("terrapod.config.settings", mock_settings)

    def test_none_request_returns_callback(self) -> None:
        with self._patched_settings():
            assert _request_base_url(None) == self._FALLBACK

    def test_clean_xfh_returns_constructed_url(self) -> None:
        req = _request_with(
            {"x-forwarded-host": "terrapod.example.com", "x-forwarded-proto": "https"}
        )
        with self._patched_settings():
            assert _request_base_url(req) == "https://terrapod.example.com"

    def test_xfh_with_port_accepted(self) -> None:
        req = _request_with(
            {"x-forwarded-host": "terrapod.example.com:8443", "x-forwarded-proto": "https"}
        )
        with self._patched_settings():
            assert _request_base_url(req) == "https://terrapod.example.com:8443"

    def test_xfh_with_comma_takes_first_entry(self) -> None:
        # Standard XFH chain (multi-proxy). We take the leftmost (client-
        # nearest) value and validate it.
        req = _request_with(
            {
                "x-forwarded-host": "terrapod.example.com, internal-proxy.example.com",
                "x-forwarded-proto": "https",
            }
        )
        with self._patched_settings():
            assert _request_base_url(req) == "https://terrapod.example.com"

    def test_crlf_in_xfh_falls_back(self) -> None:
        """The attack case. A CRLF-injected XFH must NOT make it into
        the returned URL — fall back to the configured callback."""
        req = _request_with(
            {
                "x-forwarded-host": "evil.com\r\nLocation: https://attacker/",
                "x-forwarded-proto": "https",
            }
        )
        with self._patched_settings():
            assert _request_base_url(req) == self._FALLBACK

    def test_unsafe_scheme_falls_back(self) -> None:
        req = _request_with(
            {"x-forwarded-host": "terrapod.example.com", "x-forwarded-proto": "javascript"}
        )
        with self._patched_settings():
            assert _request_base_url(req) == self._FALLBACK

    def test_host_header_with_dot_used_when_xfh_missing(self) -> None:
        req = _request_with({"host": "terrapod.example.com"})
        with self._patched_settings():
            assert _request_base_url(req) == "https://terrapod.example.com"

    def test_service_dns_host_skipped(self) -> None:
        """Service-DNS hostnames like `terrapod-api:8000` have no `.`
        and are skipped — emitting them would publish a URL only the
        API pod itself can resolve."""
        req = _request_with({"host": "terrapod-api:8000"})
        with self._patched_settings():
            assert _request_base_url(req) == self._FALLBACK

    def test_crlf_in_host_falls_back(self) -> None:
        req = _request_with({"host": "evil.com\r\nfoo: bar"})
        with self._patched_settings():
            assert _request_base_url(req) == self._FALLBACK
