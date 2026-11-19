"""Admission-time attestation checking for mirrored OCI content (#2589).

These exist because the whole blast radius passes without them. Adding the
check to both admission paths broke not one of the 221 existing OCI and warm
tests -- because with no upstream signer configured the gate returns
`none-configured` immediately, and a test that configures nothing takes that
path. So the gate shipped green and unexercised, which is the shape AGENTS.md
names: an optional path makes the bypass the default, and tests take defaults.

Every test here therefore configures a signer, which is the only way to reach
the code under test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.config import OCIUpstreamConfig
from terrapod.services import sigstore_verification as sigver
from terrapod.services.oci import attestation as att

HOST = "ghcr.io"
REPO = "org/image"
DIGEST = "sha256:" + "11" * 32
IDENTITY = "https://github.com/org/repo/.github/workflows/release.yml@refs/tags/v1"
ISSUER = "https://token.actions.githubusercontent.com"


def _upstream(**kwargs) -> OCIUpstreamConfig:
    return OCIUpstreamConfig(host=HOST, **kwargs)


def _signed(**kwargs) -> OCIUpstreamConfig:
    return _upstream(signer_identity=IDENTITY, signer_issuer=ISSUER, **kwargs)


def _with_upstreams(*upstreams):
    """Point `settings.registry.oci.upstreams` at these for one test."""
    return patch(
        "terrapod.config.settings.registry.oci.upstreams",
        list(upstreams),
    )


def _bundle(predicate: str = "https://slsa.dev/provenance/v1") -> att.DiscoveredBundle:
    return att.DiscoveredBundle(digest="sha256:" + "ab" * 32, size=800, predicate=predicate)


class TestAnUpstreamWithNoSignerIsNotChecked:
    """Not a failure and not a pass -- a third, honest answer.

    "We did not check" and "we checked and nobody signed it" are different
    facts, and collapsing them into a boolean loses the one an auditor needs.
    """

    @pytest.mark.asyncio
    async def test_an_unconfigured_upstream_reports_none_configured(self) -> None:
        with _with_upstreams(_upstream()):
            with patch.object(att, "discover_bundles", new=AsyncMock()) as discover:
                got = await att.attest_before_admission(HOST, REPO, DIGEST)
        assert got == att.ATTEST_NOT_CONFIGURED
        # And it costs nothing: no registry round trip looking for a bundle
        # nobody asked us to check.
        discover.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unknown_host_reports_none_configured(self) -> None:
        with _with_upstreams(
            OCIUpstreamConfig(host="quay.io", signer_identity=IDENTITY, signer_issuer=ISSUER)
        ):
            got = await att.attest_before_admission(HOST, REPO, DIGEST)
        assert got == att.ATTEST_NOT_CONFIGURED


class TestAnUnsignedArtifactIsAdmittedUnlessRequired:
    """Most container content in the world is unsigned.

    Refusing it by default would make this a switch nobody could turn on, so
    the default records the absence instead.
    """

    @pytest.mark.asyncio
    async def test_no_attestation_is_recorded_rather_than_refused(self) -> None:
        with _with_upstreams(_signed()):
            with patch.object(att, "discover_bundles", new=AsyncMock(return_value=[])):
                got = await att.attest_before_admission(HOST, REPO, DIGEST)
        assert got == att.ATTEST_NONE_PUBLISHED

    @pytest.mark.asyncio
    async def test_require_attestation_refuses_the_same_artifact(self) -> None:
        with _with_upstreams(_signed(require_attestation=True)):
            with patch.object(att, "discover_bundles", new=AsyncMock(return_value=[])):
                with pytest.raises(att.AttestationRefused) as caught:
                    await att.attest_before_admission(HOST, REPO, DIGEST)
        message = str(caught.value)
        assert "publishes no Sigstore attestation" in message
        # It names the signer it wanted, so the operator can see whether their
        # expectation or the upstream is the thing that is wrong.
        assert IDENTITY in message


class TestAVerifiedAttestationIsRecorded:
    @pytest.mark.asyncio
    async def test_a_bundle_that_verifies_reports_the_publisher(self) -> None:
        with _with_upstreams(_signed()):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(sigver, "verify_attestation", new=AsyncMock(return_value=("p", {}))),
            ):
                got = await att.attest_before_admission(HOST, REPO, DIGEST)
        assert got == att.ATTEST_UPSTREAM_PUBLISHER

    @pytest.mark.asyncio
    async def test_the_digest_is_passed_without_its_algorithm_prefix(self) -> None:
        """`verify_attestation` takes hex, and the in-toto subject is hex.

        Passing `sha256:...` through would fail `bytes.fromhex` and surface as
        "not a hex sha256 digest" on a perfectly good artifact.
        """
        with _with_upstreams(_signed()):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(
                    sigver, "verify_attestation", new=AsyncMock(return_value=("p", {}))
                ) as verify,
            ):
                await att.attest_before_admission(HOST, REPO, DIGEST)
        assert verify.await_args.kwargs["digest_hex"] == "11" * 32

    @pytest.mark.asyncio
    async def test_the_configured_signer_is_what_is_demanded(self) -> None:
        with _with_upstreams(_signed()):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(
                    sigver, "verify_attestation", new=AsyncMock(return_value=("p", {}))
                ) as verify,
            ):
                await att.attest_before_admission(HOST, REPO, DIGEST)
        expect = verify.await_args.kwargs["expect"]
        assert (expect.identity, expect.issuer) == (IDENTITY, ISSUER)


class TestAPresentButBadAttestationAlwaysFails:
    """The asymmetry at the heart of this, and the easiest thing to get wrong.

    `require_attestation` decides whether an **absent** attestation is
    tolerated. It never decides whether a **bad** one is. An artifact that
    publishes a signature which does not verify as the expected signer is the
    supply-chain event this feature exists to catch, so it fails closed whether
    or not the operator asked for attestations to be mandatory.
    """

    @pytest.mark.asyncio
    async def test_a_failing_bundle_is_refused_even_without_require(self) -> None:
        with _with_upstreams(_signed(require_attestation=False)):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(
                    sigver,
                    "verify_attestation",
                    new=AsyncMock(side_effect=sigver.SigstoreVerificationError("wrong signer")),
                ),
            ):
                with pytest.raises(att.AttestationRefused) as caught:
                    await att.attest_before_admission(HOST, REPO, DIGEST)
        assert "none verified" in str(caught.value)
        assert "wrong signer" in str(caught.value)

    @pytest.mark.asyncio
    async def test_several_bundles_are_tried_before_refusing(self) -> None:
        """An image carries a signature AND attestations; only one need match.

        Refusing after the first failure would reject every image whose
        signature happens not to be the first entry in the index.
        """
        good = AsyncMock(side_effect=[sigver.SigstoreVerificationError("no"), ("p", {})])
        with _with_upstreams(_signed()):
            with (
                patch.object(
                    att, "discover_bundles", new=AsyncMock(return_value=[_bundle(), _bundle("x")])
                ),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(sigver, "verify_attestation", new=good),
            ):
                got = await att.attest_before_admission(HOST, REPO, DIGEST)
        assert got == att.ATTEST_UPSTREAM_PUBLISHER
        assert good.await_count == 2

    @pytest.mark.asyncio
    async def test_a_bundle_whose_bytes_cannot_be_fetched_is_not_a_pass(self) -> None:
        with _with_upstreams(_signed()):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=None)),
            ):
                with pytest.raises(att.AttestationRefused):
                    await att.attest_before_admission(HOST, REPO, DIGEST)


class TestAnUnreachableTrustRootRefuses:
    """It says nothing about the artifact, and is still a refusal.

    Admitting unverified content on an upstream an operator asked to verify
    would be the quiet pass this feature exists to remove -- so the outcome is
    a refusal, with a message that points at the environment rather than at the
    artifact.
    """

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_trust_root_not_the_artifact(self) -> None:
        with _with_upstreams(_signed()):
            with (
                patch.object(att, "discover_bundles", new=AsyncMock(return_value=[_bundle()])),
                patch.object(att, "_bundle_bytes", new=AsyncMock(return_value=b"{}")),
                patch.object(
                    sigver,
                    "verify_attestation",
                    new=AsyncMock(side_effect=sigver.TrustRootUnavailable("no route")),
                ),
            ):
                with pytest.raises(att.AttestationRefused) as caught:
                    await att.attest_before_admission(HOST, REPO, DIGEST)
        assert "trust root was unavailable" in str(caught.value)


class TestBothAdmissionPathsUseTheGate:
    """Neither path may admit what the other refuses.

    A warm that stored what a pull would reject is a way around the control, so
    this reads the two call sites out of the source rather than trusting that
    both were wired -- the behavioural tests above drive the helper, and a
    helper can be correct while a caller forgets it.
    """

    @staticmethod
    def _source(relative: str) -> str:
        """Read a module's source off disk.

        Via `__path__` rather than `__file__`, because `terrapod` is a PEP 420
        namespace package -- it deliberately has no `__init__.py`, so
        `__file__` is None and the obvious form raises a TypeError that names
        pathlib rather than the cause.
        """
        import pathlib

        import terrapod

        return (pathlib.Path(terrapod.__path__[0]) / relative).read_text()

    def test_the_pull_through_route_calls_it_before_storing(self) -> None:
        src = self._source("api/routers/oci.py")
        assert "attest_before_admission" in src, "the pull-through route skips the gate"
        gate = src.index("attest_before_admission")
        store = src.index("store_manifest", gate)
        # Positional, because the ordering IS the property: verifying after
        # storing would leave unverified bytes servable in the window between.
        assert gate < store

    def test_the_warm_path_calls_it_before_storing(self) -> None:
        src = self._source("services/warm_ahead.py")
        assert "attest_before_admission" in src, "the warm path skips the gate"
        gate = src.index("attest_before_admission")
        store = src.index("store_manifest", gate)
        assert gate < store
