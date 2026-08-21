"""Credential-extraction tests for the OCI registry surface (#1408).

The extraction half is pure and gets exercised exhaustively here; the
resolution half (runner token / API token / session) is covered at the router
tier where the DB is mocked.

Why this warrants its own file: container clients are the *only* consumers that
authenticate with Basic, and getting it wrong fails in an especially unhelpful
way — `docker push` reports "unauthorized" with no indication of whether the
credential was rejected or never read.
"""

import base64

import pytest
from fastapi import Request

from terrapod.services.oci.auth import extract_credential


def _request(authorization: str | None) -> Request:
    """A bare ASGI scope — no app, no client, nothing but the header."""
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request({"type": "http", "method": "GET", "path": "/v2/", "headers": headers})


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class TestBasic:
    def test_password_is_the_credential(self) -> None:
        assert (
            extract_credential(_request(_basic("anything", "tok.tpod.secret"))) == "tok.tpod.secret"
        )

    def test_username_is_ignored_entirely(self) -> None:
        """The GHCR pattern: `docker login -u <anything> -p <token>`."""
        for user in ("", "x", "terrapod", "someone@example.com"):
            assert extract_credential(_request(_basic(user, "tok"))) == "tok"

    def test_password_containing_colons_survives(self) -> None:
        """Split on the *last* colon: a username may not contain one, a
        password may — and a runner token is literally colon-delimited."""
        cred = extract_credential(_request(_basic("u", "runtok:abc:123:456:sig")))
        assert cred == "runtok:abc:123:456:sig"

    def test_scheme_is_case_insensitive(self) -> None:
        raw = base64.b64encode(b"u:tok").decode()
        assert extract_credential(_request(f"basic {raw}")) == "tok"
        assert extract_credential(_request(f"BASIC {raw}")) == "tok"

    @pytest.mark.parametrize(
        "value",
        [
            "Basic ",  # nothing at all
            "Basic !!!not-base64!!!",
            "Basic " + base64.b64encode(b"no-colon-present").decode(),
            "Basic " + base64.b64encode(b"user:").decode(),  # empty password
        ],
    )
    def test_unusable_values_yield_none_not_an_exception(self, value: str) -> None:
        """Never raise on a malformed header: the caller decides what a missing
        credential means for its route."""
        assert extract_credential(_request(value)) is None

    def test_malformed_base64_is_not_distinguished_from_a_bad_password(self) -> None:
        """Both are None, so the response cannot be used as an oracle for which
        part of the credential was wrong."""
        assert extract_credential(_request("Basic !!!")) is None
        assert extract_credential(_request(_basic("u", ""))) is None


class TestBearer:
    def test_accepted_so_curl_and_the_sdk_work_unchanged(self) -> None:
        assert extract_credential(_request("Bearer tok.tpod.secret")) == "tok.tpod.secret"

    def test_case_insensitive(self) -> None:
        assert extract_credential(_request("bearer tok")) == "tok"

    def test_empty_bearer_is_none(self) -> None:
        assert extract_credential(_request("Bearer ")) is None


class TestAbsentAndUnknown:
    def test_no_header(self) -> None:
        assert extract_credential(_request(None)) is None

    def test_empty_header(self) -> None:
        assert extract_credential(_request("")) is None

    @pytest.mark.parametrize("value", ["Digest abc", "Negotiate abc", "token abc", "abc"])
    def test_unknown_schemes_are_ignored(self, value: str) -> None:
        """Including `token abc` — Pulumi's scheme — which must not be honoured
        here just because Terrapod will speak it elsewhere."""
        assert extract_credential(_request(value)) is None
