"""Error-envelope tests for the OCI registry surface (#1408).

The shape here is dictated by an unchangeable client, so it is deliberately not
Terrapod's house JSON:API envelope — the same exemption the runner protocol and
the OAuth token endpoints have. These tests pin that, because a well-meaning
future change to "make errors consistent" would break `docker push` in a way
that surfaces to the operator as an unhelpful "unknown error".
"""

import json

from terrapod.services.oci.errors import (
    BLOB_UNKNOWN,
    DENIED,
    DIGEST_INVALID,
    MANIFEST_UNKNOWN,
    NAME_UNKNOWN,
    UNAUTHORIZED,
    OCIError,
    oci_error_body,
    oci_error_response,
)


class TestEnvelopeShape:
    def test_is_an_errors_array_not_a_detail_object(self) -> None:
        body = oci_error_body(BLOB_UNKNOWN)
        assert set(body) == {"errors"}
        assert isinstance(body["errors"], list)
        assert body["errors"][0]["code"] == "BLOB_UNKNOWN"
        assert "detail" not in body  # never the house shape

    def test_detail_is_omitted_when_absent_rather_than_null(self) -> None:
        assert "detail" not in oci_error_body(BLOB_UNKNOWN)["errors"][0]

    def test_detail_is_included_when_given(self) -> None:
        body = oci_error_body(BLOB_UNKNOWN, detail={"digest": "sha256:abc"})
        assert body["errors"][0]["detail"] == {"digest": "sha256:abc"}

    def test_message_can_be_overridden_for_a_useful_one(self) -> None:
        """The spec's stock wording is correct but says nothing about *which*
        blob, which is the only thing an operator wants to know."""
        body = oci_error_body(BLOB_UNKNOWN, message="no such blob: sha256:abc")
        assert body["errors"][0]["message"] == "no such blob: sha256:abc"


class TestStatusPairing:
    def test_codes_carry_the_status_the_spec_pairs_them_with(self) -> None:
        assert BLOB_UNKNOWN.status_code == 404
        assert MANIFEST_UNKNOWN.status_code == 404
        assert NAME_UNKNOWN.status_code == 404
        assert DIGEST_INVALID.status_code == 400
        assert UNAUTHORIZED.status_code == 401
        assert DENIED.status_code == 403

    def test_response_uses_the_paired_status(self) -> None:
        assert oci_error_response(MANIFEST_UNKNOWN).status_code == 404
        assert oci_error_response(DENIED).status_code == 403

    def test_response_body_is_the_envelope(self) -> None:
        response = oci_error_response(DIGEST_INVALID, detail={"expected": "sha256:aa"})
        payload = json.loads(bytes(response.body))
        assert payload["errors"][0]["code"] == "DIGEST_INVALID"
        assert payload["errors"][0]["detail"] == {"expected": "sha256:aa"}

    def test_headers_pass_through(self) -> None:
        """Some errors must carry headers — a 401 needs its challenge."""
        response = oci_error_response(UNAUTHORIZED, headers={"WWW-Authenticate": 'Basic realm="t"'})
        assert response.headers["WWW-Authenticate"] == 'Basic realm="t"'


class TestOCIError:
    def test_carries_code_so_the_raise_site_cannot_mismatch_the_status(self) -> None:
        err = OCIError(MANIFEST_UNKNOWN)
        assert err.error.code == "MANIFEST_UNKNOWN"
        assert err.error.status_code == 404

    def test_defaults_to_the_specs_message(self) -> None:
        assert OCIError(BLOB_UNKNOWN).message == BLOB_UNKNOWN.message

    def test_accepts_an_override_and_detail(self) -> None:
        err = OCIError(BLOB_UNKNOWN, detail={"digest": "sha256:abc"}, message="gone")
        assert err.message == "gone"
        assert err.detail == {"digest": "sha256:abc"}

    def test_str_names_the_code(self) -> None:
        assert "MANIFEST_UNKNOWN" in str(OCIError(MANIFEST_UNKNOWN))
