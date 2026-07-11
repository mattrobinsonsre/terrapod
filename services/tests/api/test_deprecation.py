"""Tests for the deprecation-header helper (#550)."""

from __future__ import annotations

from datetime import date

from fastapi import Response

from terrapod.api.deprecation import DEPRECATIONS_DOC_URL, _imf_fixdate, mark_deprecated


def test_imf_fixdate_is_rfc7231_gmt() -> None:
    assert _imf_fixdate(date(2027, 1, 1)) == "Fri, 01 Jan 2027 00:00:00 GMT"


def test_mark_deprecated_sets_all_three_headers() -> None:
    resp = Response()
    mark_deprecated(resp, sunset=date(2027, 6, 30))

    assert resp.headers["Deprecation"] == "true"
    assert resp.headers["Sunset"] == "Wed, 30 Jun 2027 00:00:00 GMT"
    assert resp.headers["Link"] == (
        f'<{DEPRECATIONS_DOC_URL}>; rel="deprecation"; type="text/html"'
    )


def test_mark_deprecated_custom_link() -> None:
    resp = Response()
    mark_deprecated(resp, sunset=date(2027, 6, 30), link="https://example.test/x")
    assert 'rel="deprecation"' in resp.headers["Link"]
    assert "https://example.test/x" in resp.headers["Link"]


def test_mark_deprecated_does_not_touch_body_or_status() -> None:
    # A deprecated endpoint must keep working for lagging clients — headers only.
    resp = Response(content=b"payload", status_code=200)
    mark_deprecated(resp, sunset=date(2027, 6, 30))
    assert resp.body == b"payload"
    assert resp.status_code == 200
