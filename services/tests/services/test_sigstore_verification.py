"""Sigstore verification (#2589).

These run without network. The library's own verification is exercised against
real material by hand -- see the PR -- and what is pinned here is everything
*around* it, which is where this module's value and its risks are: that an
identity is required rather than defaulted, that cheap checks happen before an
expensive network refresh, that an environmental failure is reported as its own
reason, and above all that an attestation is checked to be **about** the
artifact being admitted.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from terrapod.services import sigstore_verification as sv


class TestAnIdentityAloneIsRefused:
    """An identity without an issuer is forgeable, so the type refuses it.

    Not a style preference: anyone who can obtain a certificate naming an
    identity from *some* OIDC provider satisfies an identity-only check, so
    accepting one would be a control that does nothing.
    """

    def test_an_issuer_is_required(self) -> None:
        with pytest.raises(ValueError, match="needs an issuer"):
            sv.ExpectedSigner(identity="me@example.com", issuer="")

    def test_the_refusal_says_why_rather_than_only_that(self) -> None:
        with pytest.raises(ValueError) as caught:
            sv.ExpectedSigner(identity="me@example.com", issuer="   ")
        # The reason is the useful half -- someone hitting this needs to know
        # an issuer is not boilerplate.
        assert "forgeable" in str(caught.value)

    def test_an_identity_is_required(self) -> None:
        with pytest.raises(ValueError, match="needs an identity"):
            sv.ExpectedSigner(identity="  ", issuer="https://accounts.example")

    def test_a_complete_signer_is_accepted(self) -> None:
        signer = sv.ExpectedSigner(identity="me@example.com", issuer="https://accounts.example")
        assert signer.identity == "me@example.com"


class TestLocalChecksHappenBeforeTheNetwork:
    """A malformed digest must not cost a TUF refresh, and must not be
    reported as one.

    The first version of this module obtained the trust root first, which made
    a bad digest surface as "could not obtain the Sigstore trust root" -- an
    error that sends an operator to look at their egress instead of at the
    caller. Both halves are asserted: no network, and the right reason.
    """

    @pytest.mark.parametrize(
        "bad",
        ["nothex", "ab", "ab" * 31, "", "zz" * 32],
        ids=["not-hex", "too-short", "one-byte-short", "empty", "hex-ish-but-not"],
    )
    @pytest.mark.asyncio
    async def test_a_malformed_digest_is_refused_without_touching_the_trust_root(
        self, bad: str
    ) -> None:
        signer = sv.ExpectedSigner(identity="me", issuer="https://accounts.example")
        with patch.object(sv, "_trust_config") as trust:
            with pytest.raises(sv.SigstoreVerificationError) as caught:
                await sv.verify_digest(digest_hex=bad, bundle_json=b"{}", expect=signer)
        trust.assert_not_called()
        assert "digest" in str(caught.value)

    @pytest.mark.asyncio
    async def test_the_attestation_path_checks_the_digest_first_too(self) -> None:
        signer = sv.ExpectedSigner(identity="me", issuer="https://accounts.example")
        with patch.object(sv, "_trust_config") as trust:
            with pytest.raises(sv.SigstoreVerificationError):
                await sv.verify_attestation(digest_hex="nothex", bundle_json=b"{}", expect=signer)
        trust.assert_not_called()


class TestTheSubjectIsChecked:
    """The check the module exists for.

    `verify_dsse` establishes that the expected identity signed the envelope.
    It says nothing about which artifact the envelope is about, so a genuine,
    correctly-signed attestation for a *different* artifact verifies perfectly.
    Without this check, substituting one is an attack that requires forging
    nothing.
    """

    def test_subjects_are_read_from_the_statement(self) -> None:
        statement = {
            "subject": [
                {"name": "a", "digest": {"sha256": "AA" * 32}},
                {"name": "b", "digest": {"sha256": "bb" * 32}},
                {"name": "no-sha", "digest": {"sha512": "cc" * 64}},
                "not-a-dict",
                {"name": "blank", "digest": {"sha256": "   "}},
            ]
        }
        # Case-folded, because a digest is hex and an attestation may upper-case
        # it; the blank, the non-dict and the sha512-only entry contribute
        # nothing rather than raising.
        assert sv._attested_digests(statement) == {"aa" * 32, "bb" * 32}

    def test_a_statement_with_no_subject_attests_to_nothing(self) -> None:
        assert sv._attested_digests({}) == set()
        assert sv._attested_digests({"subject": []}) == set()

    @pytest.mark.asyncio
    async def test_a_valid_attestation_about_another_artifact_is_refused(self) -> None:
        signer = sv.ExpectedSigner(identity="me", issuer="https://accounts.example")
        mine, theirs = "11" * 32, "22" * 32
        statement = {"subject": [{"digest": {"sha256": theirs}}], "predicateType": "x"}

        with self._verifier_returning(sv._IN_TOTO, statement):
            with pytest.raises(sv.SigstoreVerificationError) as caught:
                await sv.verify_attestation(digest_hex=mine, bundle_json=b"{}", expect=signer)
        message = str(caught.value)
        assert "wrong artifact" in message
        # It names what the attestation IS about, so the reader can tell a
        # substitution from a mistake.
        assert theirs in message

    @pytest.mark.asyncio
    async def test_the_matching_subject_is_accepted(self) -> None:
        signer = sv.ExpectedSigner(identity="me", issuer="https://accounts.example")
        mine = "11" * 32
        statement = {
            "subject": [{"digest": {"sha256": mine.upper()}}],
            "predicateType": "https://slsa.dev/provenance/v1",
        }
        with self._verifier_returning(sv._IN_TOTO, statement):
            predicate, got = await sv.verify_attestation(
                digest_hex=mine, bundle_json=b"{}", expect=signer
            )
        assert predicate == "https://slsa.dev/provenance/v1"
        assert got == statement

    @pytest.mark.asyncio
    async def test_an_envelope_that_is_not_in_toto_is_refused(self) -> None:
        """A verified envelope carrying something else has no subject to check.

        `verify_dsse` hands back the payload type precisely so the caller
        checks it; trusting the bytes without doing so would mean the subject
        check silently passes over a payload it cannot read.
        """
        signer = sv.ExpectedSigner(identity="me", issuer="https://accounts.example")
        with self._verifier_returning("application/vnd.something+json", {"subject": []}):
            with pytest.raises(sv.SigstoreVerificationError) as caught:
                await sv.verify_attestation(digest_hex="11" * 32, bundle_json=b"{}", expect=signer)
        assert "in-toto" in str(caught.value)

    @staticmethod
    def _verifier_returning(payload_type: str, statement: dict[str, Any]):
        """Patch the boundary BELOW this module: the library's own verifier.

        Deliberately not patching `_verify_attestation_sync`, which is where
        every check under test lives -- patching that would leave these tests
        asserting on a function that never runs.
        """
        import json

        bundle = MagicMock()
        verifier = MagicMock()
        verifier.verify_dsse.return_value = (payload_type, json.dumps(statement).encode())

        models = MagicMock()
        models.Bundle.from_json.return_value = bundle
        verify_mod = MagicMock()
        verify_mod.Verifier.return_value = verifier

        return patch.dict(
            "sys.modules",
            {
                "sigstore.models": models,
                "sigstore.verify": verify_mod,
                "sigstore.hashes": MagicMock(),
            },
        )


class TestTheTrustRootReportsItsOwnFailure:
    """An unreachable trust root says nothing about the artifact.

    Reported as a distinct exception because the two want opposite responses: a
    failed signature means reject the artifact, an unobtainable root means fix
    the environment (or mirror the TUF repository, for a sealed deployment
    re-verifying cached content).
    """

    def test_an_unobtainable_root_is_not_a_verification_failure(self) -> None:
        sv._TRUST_CACHE.clear()
        models = MagicMock()
        models.ClientTrustConfig.from_tuf.side_effect = RuntimeError("no route to host")
        with patch.dict("sys.modules", {"sigstore.models": models}):
            with pytest.raises(sv.TrustRootUnavailable) as caught:
                sv._trust_config("https://tuf.example", refresh=True, ttl_seconds=60.0)
        assert "no route to host" in str(caught.value)

    def test_it_is_still_a_sigstore_error_so_one_except_catches_both(self) -> None:
        # A caller that only wants "did this fail" should not have to name both.
        assert issubclass(sv.TrustRootUnavailable, sv.SigstoreVerificationError)

    def test_a_refresh_failure_does_not_fall_back_to_a_stale_cached_root(self) -> None:
        """The cache must not rescue a failed refresh.

        Verifying against a root we could not refresh is how an expired
        revocation goes unnoticed, so a refresh failure is a failure even when
        something usable is in hand.
        """
        sv._TRUST_CACHE.clear()
        url = "https://tuf.example"
        sv._TRUST_CACHE[url] = (time.monotonic() - 10_000.0, MagicMock())  # stale

        models = MagicMock()
        models.ClientTrustConfig.from_tuf.side_effect = RuntimeError("upstream down")
        with patch.dict("sys.modules", {"sigstore.models": models}):
            with pytest.raises(sv.TrustRootUnavailable):
                sv._trust_config(url, refresh=True, ttl_seconds=1.0)


class TestTheTrustRootIsCachedWithABound:
    """Cached because a refresh is several round trips; bounded because the
    root is the one mutable thing here and rotates.
    """

    def test_a_fresh_entry_is_reused_without_a_refresh(self) -> None:
        sv._TRUST_CACHE.clear()
        url, held = "https://tuf.example", MagicMock()
        sv._TRUST_CACHE[url] = (time.monotonic(), held)

        models = MagicMock()
        with patch.dict("sys.modules", {"sigstore.models": models}):
            got = sv._trust_config(url, refresh=True, ttl_seconds=3600.0)
        assert got is held
        models.ClientTrustConfig.from_tuf.assert_not_called()

    def test_an_expired_entry_is_refreshed(self) -> None:
        """The bound binds. A root held for ever would start refusing valid
        signatures after a Sigstore key rotation.
        """
        sv._TRUST_CACHE.clear()
        url, stale, fresh = "https://tuf.example", MagicMock(), MagicMock()
        sv._TRUST_CACHE[url] = (time.monotonic() - 7200.0, stale)

        models = MagicMock()
        models.ClientTrustConfig.from_tuf.return_value = fresh
        with patch.dict("sys.modules", {"sigstore.models": models}):
            got = sv._trust_config(url, refresh=True, ttl_seconds=3600.0)
        assert got is fresh and got is not stale
        models.ClientTrustConfig.from_tuf.assert_called_once()

    def test_refresh_false_asks_the_library_for_offline(self) -> None:
        """The sealed-deployment path: use the persisted TUF cache, no network."""
        sv._TRUST_CACHE.clear()
        models = MagicMock()
        models.ClientTrustConfig.from_tuf.return_value = MagicMock()
        with patch.dict("sys.modules", {"sigstore.models": models}):
            sv._trust_config("https://tuf.example", refresh=False, ttl_seconds=60.0)
        _, kwargs = models.ClientTrustConfig.from_tuf.call_args
        assert kwargs["offline"] is True
