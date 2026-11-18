"""Publisher provenance for the package caches (#2589, Gap A).

The fixtures are REAL material captured from the live registries, because the
defect these exist to catch is invisible against an invented one: npm's signed
message is a specific canonical string, so an ECDSA signature over anything else
simply fails and a made-up fixture would test the arithmetic rather than the
format.

They also exist because the whole admission path passed WITHOUT them. Adding the
hook to pypi and npm broke not one of 505 existing package-cache tests, and an
instrumented run proved why: neither verify function was reached even once. The
per-ecosystem files drive those modules in isolation and the substrate's
miss-path tests use no real ecosystem resolver, so the hook shipped green and
unexercised -- the shape AGENTS.md names. Every test here reaches the code under
test.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from terrapod.services.package_cache import publisher_provenance as pp

#: npm's real version document for the fixture package, trimmed to what the
#: check reads, plus the keys the registry publishes.
NPM_DOC = json.loads(
    r"""{"dist": {"integrity": "sha512-PHMifhh3EN4loMcHCz6l3v/luzgT3za+9f8subGgeMNjbJjzH4Ij/YoX3Gvu+kaouJRIlVdTHHCREADYf+ZteA==", "signatures": [{"sig": "MEUCIFFWJhn8uYavdTFhFBe51TdFVm2BWZTpNU8XXDyKqjHEAiEA6ueK9t6wUnn5DqL9LA+25JRseNOfzRPsCWLULd2m3Fs=", "keyid": "SHA256:jl3bwswu80PjjokCgh0o2w5c2U4LhQAE57gj9cz1kzA"}], "tarball": "https://registry.npmjs.org/sigstore/-/sigstore-3.0.0.tgz"}}"""
)
NPM_KEYS = json.loads(
    r"""{"keys": [{"keyid": "SHA256:jl3bwswu80PjjokCgh0o2w5c2U4LhQAE57gj9cz1kzA", "key": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE1Olb3zMAFFxXKHiIkQO5cJ3Yhl5i6UPp+IhuteBJbuHcA5UogKo0EWtlWwW6KSaKoTNEYL7JlCQiVnkhBktUgg=="}, {"keyid": "SHA256:DhQ8wR5APBvFHLF/+Tc+AYvPOdTpcIDqOhxsBHRwC7U", "key": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEY6Ya7W++7aUPzvMTrezH6Ycx3c+HOKYCcNGybJZSCJq/fd7Qa8uuAKtdIkUQtQiEKERhAmE5lMMJhP8OkDOa2g=="}]}"""
)
NPM_NAME = "sigstore"
NPM_VERSION = "3.0.0"


def _client(handler) -> httpx.AsyncClient:
    """A real AsyncClient over a MockTransport.

    A real client rather than a mock, so the code under test exercises the
    actual request/response path it will use in production -- including the
    status-code branches, which a MagicMock would simply agree with.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _keys_handler(payload=None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/-/npm/v1/keys"), request.url
        return httpx.Response(status, json=payload if payload is not None else NPM_KEYS)

    return handler


@pytest.mark.asyncio
class TestNpmRegistrySignature:
    """The npm half, verified against the registry's own published key.

    This is the achievable npm check. Its *provenance* attestations are the
    stronger claim and cannot be verified here at all -- their transparency-log
    entries are `intoto 0.0.2` and sigstore-python 4.5.1, the current release,
    accepts only `dsse`/`hashedrekord` `0.0.1`. Measured, not assumed.
    """

    async def test_a_real_signature_verifies(self) -> None:
        async with _client(_keys_handler()) as client:
            got = await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=NPM_DOC)
        assert got is not None
        assert got.predicate == "npm-registry-signature"
        # No per-package identity: the REGISTRY signed it, and inventing one
        # would record a publisher claim nobody made.
        assert got.identity == ""
        assert got.issuer == ""

    async def test_a_tampered_integrity_is_refused(self) -> None:
        """The control that makes the test above mean something.

        The signed message is `<name>@<version>:<integrity>`, so changing the
        integrity changes what was signed. Without this, a verifier that
        returned success unconditionally would pass every other test here.
        """
        bad = json.loads(json.dumps(NPM_DOC))
        bad["dist"]["integrity"] = bad["dist"]["integrity"][:-4] + "AAAA"
        async with _client(_keys_handler()) as client:
            with pytest.raises(pp.ProvenanceError, match="does not verify"):
                await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=bad)

    async def test_a_wrong_version_is_refused(self) -> None:
        """The version is in the signed message too, so a substituted version
        document cannot be passed off as another release."""
        async with _client(_keys_handler()) as client:
            with pytest.raises(pp.ProvenanceError):
                await pp.verify_npm(client, name=NPM_NAME, version="99.99.99", document=NPM_DOC)

    async def test_a_package_with_no_signature_is_not_a_failure(self) -> None:
        """Most of npm's history predates registry signing."""
        unsigned = {"dist": {"integrity": NPM_DOC["dist"]["integrity"]}}
        async with _client(_keys_handler()) as client:
            assert (
                await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=unsigned)
                is None
            )

    async def test_no_integrity_means_no_message_to_verify(self) -> None:
        """A package published before `integrity` existed carries only `shasum`,
        and the signed message cannot be formed without it."""
        legacy = {"dist": {"signatures": NPM_DOC["dist"]["signatures"], "shasum": "abc"}}
        async with _client(_keys_handler()) as client:
            assert (
                await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=legacy)
                is None
            )

    async def test_a_signature_naming_an_unpublished_key_is_refused(self) -> None:
        """Not something to shrug at: it is either a rotation we have not seen or
        material we should not trust, and both want a human rather than a
        silently-skipped check."""
        async with _client(_keys_handler(payload={"keys": []})) as client:
            with pytest.raises(pp.ProvenanceError, match="does not publish"):
                await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=NPM_DOC)

    async def test_an_unreachable_key_endpoint_is_unavailable_not_a_failure(self) -> None:
        """It says nothing about the artifact, so it must not read as a bad
        signature -- the caller degrades to the digest check instead of refusing
        an artifact on the strength of our own egress."""
        async with _client(_keys_handler(payload={}, status=503)) as client:
            with pytest.raises(pp.ProvenanceUnavailable):
                await pp.verify_npm(client, name=NPM_NAME, version=NPM_VERSION, document=NPM_DOC)

    async def test_the_two_failure_kinds_are_distinguishable(self) -> None:
        """A caller must be able to tell "bad signature" from "could not ask",
        because the right response to each is opposite."""
        assert not issubclass(pp.ProvenanceUnavailable, pp.ProvenanceError)
        assert not issubclass(pp.ProvenanceError, pp.ProvenanceUnavailable)


#: A PEP 740 response shaped exactly as PyPI's integrity endpoint returns one.
#: The certificate and signature are placeholders: what is under test here is the
#: ASSEMBLY and the subject check, and the Sigstore verification itself is
#: patched at the library boundary below. Driving real crypto would make these
#: tests need network and a live trust root.
def _pep740(
    subject_sha256: str, *, predicate: str = "https://docs.pypi.org/attestations/publish/v1"
):
    import base64

    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "x-1.0.whl", "digest": {"sha256": subject_sha256}}],
        "predicateType": predicate,
    }
    return {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {"kind": "GitHub", "repository": "o/r", "workflow": "release.yml"},
                "attestations": [
                    {
                        "version": 1,
                        "envelope": {
                            "signature": base64.b64encode(b"sig").decode(),
                            "statement": base64.b64encode(json.dumps(statement).encode()).decode(),
                        },
                        "verification_material": {
                            "certificate": base64.b64encode(b"der").decode(),
                            "transparency_entries": [{"logIndex": "1"}],
                        },
                    }
                ],
            }
        ],
    }


def _provenance_handler(payload, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/integrity/" in request.url.path, request.url
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    return handler


def _verified_as(identity: str, issuer: str, predicate: str, statement: dict):
    """Patch the two boundaries BELOW the code under test.

    `_publisher_of` reads a real certificate and `_verify_pep740_sync` drives the
    Sigstore library; both need real material and a trust root. Patching them
    leaves the assembly, the subject check and the error mapping -- which is
    where this module's own logic lives -- actually running.
    """
    return (
        patch.object(pp, "_publisher_of", return_value=(identity, issuer)),
        patch.object(pp, "_verify_pep740_sync", return_value=(predicate, statement)),
    )


@pytest.mark.asyncio
class TestPypiPep740:
    DIGEST = "ab" * 32

    async def test_an_attestation_about_this_artifact_verifies(self) -> None:
        statement = {"subject": [{"digest": {"sha256": self.DIGEST}}]}
        a, b = _verified_as(
            "https://github.com/o/r/.github/workflows/release.yml@refs/heads/main",
            "https://token.actions.githubusercontent.com",
            "pub/v1",
            statement,
        )
        async with _client(_provenance_handler(_pep740(self.DIGEST))) as client:
            with a, b:
                got = await pp.verify_pypi(
                    client,
                    base_url="https://pypi.org",
                    name="x",
                    version="1.0",
                    filename="x-1.0.whl",
                    digest_hex=self.DIGEST,
                )
        assert got is not None
        assert got.predicate == "pub/v1"
        assert got.identity.endswith("release.yml@refs/heads/main")
        assert got.mechanism == "PEP 740 / Sigstore"

    async def test_a_valid_attestation_about_another_artifact_is_refused(self) -> None:
        """The check this module exists for.

        `verify_dsse` establishes that an identity signed the envelope and says
        NOTHING about which artifact the envelope is about -- so a genuine,
        correctly-signed attestation for a different file verifies perfectly.
        Without the subject check, substituting one is an attack that requires
        forging nothing.
        """
        theirs = "cd" * 32
        statement = {"subject": [{"digest": {"sha256": theirs}}]}
        a, b = _verified_as("i", "s", "pub/v1", statement)
        async with _client(_provenance_handler(_pep740(theirs))) as client:
            with a, b:
                with pytest.raises(pp.ProvenanceError) as caught:
                    await pp.verify_pypi(
                        client,
                        base_url="https://pypi.org",
                        name="x",
                        version="1.0",
                        filename="x-1.0.whl",
                        digest_hex=self.DIGEST,
                    )
        message = str(caught.value)
        assert "wrong artifact" in message
        # It names what the attestation IS about, so a reader can tell a
        # substitution from a mistake.
        assert theirs in message

    async def test_the_subject_digest_is_compared_case_insensitively(self) -> None:
        """A digest is hex and an attestation may upper-case it. Comparing
        case-sensitively would refuse a correct attestation."""
        statement = {"subject": [{"digest": {"sha256": self.DIGEST.upper()}}]}
        a, b = _verified_as("i", "s", "pub/v1", statement)
        async with _client(_provenance_handler(_pep740(self.DIGEST))) as client:
            with a, b:
                assert (
                    await pp.verify_pypi(
                        client,
                        base_url="https://pypi.org",
                        name="x",
                        version="1.0",
                        filename="x-1.0.whl",
                        digest_hex=self.DIGEST,
                    )
                    is not None
                )

    async def test_a_404_means_nothing_published(self) -> None:
        """The ordinary case for most of PyPI, and it must not read as a
        failure."""
        async with _client(_provenance_handler(None, status=404)) as client:
            assert (
                await pp.verify_pypi(
                    client,
                    base_url="https://pypi.org",
                    name="x",
                    version="1.0",
                    filename="x-1.0.whl",
                    digest_hex=self.DIGEST,
                )
                is None
            )

    async def test_an_empty_bundle_list_means_nothing_published(self) -> None:
        """A 200 with nothing in it: what PyPI returns for a project that has
        provenance for some files and not this one."""
        async with _client(
            _provenance_handler({"version": 1, "attestation_bundles": []})
        ) as client:
            assert (
                await pp.verify_pypi(
                    client,
                    base_url="https://pypi.org",
                    name="x",
                    version="1.0",
                    filename="x-1.0.whl",
                    digest_hex=self.DIGEST,
                )
                is None
            )

    async def test_a_5xx_is_unavailable_not_a_failure(self) -> None:
        async with _client(_provenance_handler(None, status=503)) as client:
            with pytest.raises(pp.ProvenanceUnavailable):
                await pp.verify_pypi(
                    client,
                    base_url="https://pypi.org",
                    name="x",
                    version="1.0",
                    filename="x-1.0.whl",
                    digest_hex=self.DIGEST,
                )

    async def test_a_certificate_naming_no_identity_is_refused(self) -> None:
        """An identity with no issuer is forgeable and an issuer with no identity
        matches nothing, so neither is something a policy could ever act on."""
        statement = {"subject": [{"digest": {"sha256": self.DIGEST}}]}
        with patch.object(pp, "_publisher_of", return_value=("", "")):
            async with _client(_provenance_handler(_pep740(self.DIGEST))) as client:
                with pytest.raises(pp.ProvenanceError, match="nothing a policy"):
                    await pp.verify_pypi(
                        client,
                        base_url="https://pypi.org",
                        name="x",
                        version="1.0",
                        filename="x-1.0.whl",
                        digest_hex=self.DIGEST,
                    )
        assert statement  # fixture used above

    async def test_an_attestation_missing_its_certificate_is_refused(self) -> None:
        payload = _pep740(self.DIGEST)
        del payload["attestation_bundles"][0]["attestations"][0]["verification_material"][
            "certificate"
        ]
        async with _client(_provenance_handler(payload)) as client:
            with pytest.raises(pp.ProvenanceError, match="no certificate"):
                await pp.verify_pypi(
                    client,
                    base_url="https://pypi.org",
                    name="x",
                    version="1.0",
                    filename="x-1.0.whl",
                    digest_hex=self.DIGEST,
                )

    async def test_the_assembled_bundle_is_a_v03_dsse_bundle(self) -> None:
        """What gets handed to the library, pinned.

        PEP 740 does not ship a Sigstore bundle; this module assembles one. If
        the assembly drifts, `Bundle.from_json` rejects it at runtime and the
        only symptom is every PyPI attestation becoming unverifiable -- so the
        shape is asserted here rather than discovered in production.
        """
        payload = _pep740(self.DIGEST)
        attestation = payload["attestation_bundles"][0]["attestations"][0]
        assembled = json.loads(pp._bundle_from_pep740(attestation))
        assert assembled["mediaType"].endswith("bundle.v0.3+json")
        assert assembled["dsseEnvelope"]["payloadType"] == "application/vnd.in-toto+json"
        assert assembled["verificationMaterial"]["certificate"]["rawBytes"]
        assert assembled["verificationMaterial"]["tlogEntries"] == [{"logIndex": "1"}]
        assert assembled["dsseEnvelope"]["signatures"] == [
            {"sig": attestation["envelope"]["signature"]}
        ]
