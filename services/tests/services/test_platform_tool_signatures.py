"""Verifying the publisher's signature on a platform tool (#2589).

`platform_tools.verify` had no `signature` level, and its own description said
none of these publishers signs anything. Measured against the live releases,
that was false for three of the four -- and the most interesting of the three is
**checkov, which Terrapod itself builds and publishes** because upstream stopped
shipping binaries. `checkov-build.yml` has attested its build provenance since
#2066; nothing ever verified it. The signature was already being produced; the
gap was on the reading side.

The property these exist to protect is the distinction at the bottom of this
file: a signature that genuinely FAILS must refuse the artifact, while signing
material that cannot be FETCHED may degrade to the checksum that already passed.
Collapsing those two -- in either direction -- is the whole risk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import platform_tool_signatures as sigs


class TestSignerSpecs:
    """Who must have signed, measured from the certificates themselves."""

    def test_opa_is_absent_rather_than_disabled(self) -> None:
        """Upstream publishes only `.sha256` files -- there is no signature in
        existence to check. An entry with verification switched off would read
        as a choice we made; absence is the honest shape, and the capability
        call reports it as a fact about the publisher."""
        assert "opa" not in sigs.SIGNERS
        assert sigs.signature_capability("opa") == sigs.NONE_PUBLISHED
        assert sigs.expected_signer("opa", "1.0.0") is None

    @pytest.mark.parametrize("tool", ["trivy", "syft", "checkov"])
    def test_the_signed_tools_resolve_to_a_signer(self, tool: str) -> None:
        assert sigs.signature_capability(tool) == "signature"
        signer = sigs.expected_signer(tool, "1.2.3")
        assert signer is not None
        assert signer.issuer == sigs.GITHUB_OIDC

    def test_trivys_identity_carries_its_release_tag(self) -> None:
        """Measured: trivy signs from `refs/tags/v<version>`, so one constant
        identity would fail on every release but the one it was written for.
        syft and checkov sign from `refs/heads/main` and are fixed."""
        a = sigs.expected_signer("trivy", "0.76.2").identity
        b = sigs.expected_signer("trivy", "0.77.0").identity
        assert a != b
        assert a.endswith("@refs/tags/v0.76.2")

        for stable in ("syft", "checkov"):
            assert (
                sigs.expected_signer(stable, "1.0.0").identity
                == sigs.expected_signer(stable, "2.0.0").identity
            )

    def test_checkov_is_verified_against_our_own_ci(self) -> None:
        """The reason this tool can be signature-verified at all. If this ever
        points somewhere else, the chain of custody for a binary every security
        scan executes has moved -- which is a decision, not a refactor."""
        signer = sigs.expected_signer("checkov", "3.4.5")
        assert signer.identity.startswith(f"https://github.com/{sigs.CHECKOV_REPO}/")
        assert "checkov-build.yml" in signer.identity
        assert sigs.SIGNERS["checkov"].material == "attestation"

    def test_every_signer_names_an_issuer(self) -> None:
        """An identity alone is forgeable: anybody can obtain a Fulcio
        certificate. The issuer is what binds it."""
        for tool, spec in sigs.SIGNERS.items():
            assert spec.issuer, tool


def _resp(status: int = 200, content: bytes = b"", payload: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.json = MagicMock(return_value=payload or {})
    return r


@pytest.mark.asyncio
class TestVerifySignature:
    async def test_a_tool_that_publishes_nothing_is_reported_not_failed(self) -> None:
        out = await sigs.verify_signature(
            MagicMock(),
            tool="opa",
            version="1.0.0",
            asset="opa_linux_amd64",
            base="https://x",
            asset_digest="ab" * 32,
        )
        assert out == sigs.NONE_PUBLISHED

    async def test_the_checksums_manifest_is_hashed_as_served(self) -> None:
        """The signature is over the manifest's BYTES. Hashing a
        re-serialisation, or the text after a decoding round-trip, verifies
        something the publisher never signed."""
        import hashlib

        manifest = b"abc123  trivy_0.76.2_linux_amd64.tar.gz\n"
        seen: dict = {}

        async def _verify(*, digest_hex, bundle_json, expect, **_kw):
            seen["digest"] = digest_hex
            seen["identity"] = expect.identity

        with patch(
            "terrapod.http_retry.arequest_with_retry",
            new=AsyncMock(side_effect=[_resp(200, manifest), _resp(200, b"{}")]),
        ):
            with patch("terrapod.services.sigstore_verification.verify_digest", new=_verify):
                out = await sigs.verify_signature(
                    MagicMock(),
                    tool="trivy",
                    version="0.76.2",
                    asset="trivy_0.76.2_linux_amd64.tar.gz",
                    base="https://x",
                    asset_digest="ab" * 32,
                )

        assert out == sigs.VERIFIED
        assert seen["digest"] == hashlib.sha256(manifest).hexdigest()
        assert seen["identity"].endswith("@refs/tags/v0.76.2")

    async def test_the_attestation_is_checked_against_the_assets_own_digest(self) -> None:
        """For checkov the statement's subject IS the asset, so there is no
        manifest hop -- and the digest handed to the verifier must be the
        asset's, not something re-derived."""
        digest = "cd" * 32
        seen: dict = {}

        async def _verify(*, digest_hex, bundle_json, expect, **_kw):
            seen["digest"] = digest_hex
            return ("https://slsa.dev/provenance/v1", {})

        payload = {"attestations": [{"bundle": {"mediaType": "x"}}]}
        with patch(
            "terrapod.http_retry.arequest_with_retry",
            new=AsyncMock(return_value=_resp(200, payload=payload)),
        ):
            with patch("terrapod.services.sigstore_verification.verify_attestation", new=_verify):
                out = await sigs.verify_signature(
                    MagicMock(),
                    tool="checkov",
                    version="3.4.5",
                    asset="checkov_linux_X86_64.zip",
                    base="https://x",
                    asset_digest=digest,
                )

        assert out == sigs.VERIFIED
        assert seen["digest"] == digest

    async def test_missing_signing_material_is_unavailable_not_success(self) -> None:
        """A 404 on the bundle must never read as verified."""
        from terrapod.services.sigstore_verification import TrustRootUnavailable

        with patch(
            "terrapod.http_retry.arequest_with_retry",
            new=AsyncMock(side_effect=[_resp(200, b"x"), _resp(404)]),
        ):
            with pytest.raises(TrustRootUnavailable):
                await sigs.verify_signature(
                    MagicMock(),
                    tool="syft",
                    version="1.57.2",
                    asset="a",
                    base="https://x",
                    asset_digest="ab" * 32,
                )

    async def test_an_empty_attestation_list_is_unavailable(self) -> None:
        from terrapod.services.sigstore_verification import TrustRootUnavailable

        with patch(
            "terrapod.http_retry.arequest_with_retry",
            new=AsyncMock(return_value=_resp(200, payload={"attestations": []})),
        ):
            with pytest.raises(TrustRootUnavailable):
                await sigs.verify_signature(
                    MagicMock(),
                    tool="checkov",
                    version="3.4.5",
                    asset="a",
                    base="https://x",
                    asset_digest="ab" * 32,
                )


@pytest.mark.asyncio
class TestTheLevelWiring:
    """`verify_platform_tool` at each level, and the distinction that matters.

    **A signature that genuinely fails must refuse the artifact; signing
    material that cannot be FETCHED may degrade to the checksum that already
    passed.** Collapsing those is the whole risk: tolerate the first and an
    attacker's substituted binary is cached, refuse the second and an air-gapped
    mirror can install nothing.
    """

    async def _run(self, level: str, **patches):
        from terrapod.services import platform_tools

        with patch.object(
            platform_tools, "_expected_sha256", new=AsyncMock(return_value="ab" * 32)
        ):
            with patch.object(platform_tools, "_mirror", return_value="https://x"):
                with patch.object(
                    platform_tools, "_asset_name", return_value="trivy_0.76.2_linux_amd64.tar.gz"
                ):
                    with patch(
                        "terrapod.services.platform_tool_signatures.verify_signature", **patches
                    ):
                        return await platform_tools.verify_platform_tool(
                            MagicMock(),
                            "trivy",
                            "0.76.2",
                            "linux",
                            "amd64",
                            {"sha256": "ab" * 32},
                            level=level,
                        )

    async def test_off_skips_everything(self) -> None:
        from terrapod.services import platform_tools

        out = await platform_tools.verify_platform_tool(
            MagicMock(), "trivy", "0.76.2", "linux", "amd64", {}, level="off"
        )
        assert out == "off"

    async def test_checksum_does_not_reach_the_signature_path(self) -> None:
        """An operator who asked for checksum must not pay a network round trip
        to a transparency log on every download."""
        called = AsyncMock(return_value=sigs.VERIFIED)
        out = await self._run("checksum", new=called)
        assert out == "checksum"
        assert called.await_count == 0

    async def test_signature_reports_what_was_established(self) -> None:
        out = await self._run("signature", new=AsyncMock(return_value=sigs.VERIFIED))
        assert out == sigs.VERIFIED

    async def test_an_unsigned_tool_reports_none_published_not_verified(self) -> None:
        """The reason the level returns a value at all: with opa in the fleet,
        a setting that answered the same for both would tell an operator their
        whole estate is signature-verified when one tool is not."""
        out = await self._run("signature", new=AsyncMock(return_value=sigs.NONE_PUBLISHED))
        assert out == sigs.NONE_PUBLISHED

    async def test_unreachable_signing_material_degrades_and_records(self) -> None:
        """A transport failure says nothing about the bytes, and the checksum
        already passed. Refusing here would make an air-gapped mirror unable to
        install a tool whose integrity is not in question."""
        from terrapod.services.sigstore_verification import TrustRootUnavailable

        out = await self._run(
            "signature", new=AsyncMock(side_effect=TrustRootUnavailable("no trust root"))
        )
        assert out == sigs.UNAVAILABLE

    async def test_a_signature_that_fails_is_never_tolerated(self) -> None:
        """The one that must not degrade. A verification failure means somebody
        other than the expected signer produced these bytes -- the artifact is
        refused and the caller caches nothing."""
        from terrapod.services.sigstore_verification import SigstoreVerificationError

        with pytest.raises(SigstoreVerificationError):
            await self._run(
                "signature",
                new=AsyncMock(side_effect=SigstoreVerificationError("wrong signer")),
            )

    async def test_a_checksum_mismatch_still_refuses_before_any_signature_work(self) -> None:
        """Order matters: the cheap local check runs first, so a tampered
        artifact is refused without a network round trip."""
        from terrapod.services import platform_tools

        called = AsyncMock(return_value=sigs.VERIFIED)
        with patch.object(
            platform_tools, "_expected_sha256", new=AsyncMock(return_value="ab" * 32)
        ):
            with patch("terrapod.services.platform_tool_signatures.verify_signature", new=called):
                with pytest.raises(platform_tools.VerificationError):
                    await platform_tools.verify_platform_tool(
                        MagicMock(),
                        "trivy",
                        "0.76.2",
                        "linux",
                        "amd64",
                        {"sha256": "cd" * 32},
                        level="signature",
                    )
        assert called.await_count == 0
