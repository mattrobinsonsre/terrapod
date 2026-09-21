"""Signed capability URLs (GHSA-r9v9-24fv-jxm2, GHSA-63m3-56rj-qqfh)."""

import base64
import time
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from terrapod.auth import capability_urls as cu


def _ca_with_key(seed: bytes):
    """A real CertificateAuthority on a deterministic key.

    Deliberately not a MagicMock: a mock has to name the attribute the module
    reads, so it goes on passing while resembling a CA less and less.
    """
    from terrapod.auth.ca import CertificateAuthority

    key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    real = CertificateAuthority.generate()
    return CertificateAuthority(ca_cert=real.ca_cert, ca_key=key)


@pytest.fixture(autouse=True)
def _ca():
    with patch("terrapod.auth.ca.get_ca", return_value=_ca_with_key(bytes(range(32)))):
        yield


class TestRoundTrip:
    def test_a_minted_capability_names_its_resource(self):
        cap = cu.mint("plan-log", "run-abc")
        assert cu.verify(cap, expect_kind="plan-log") == "run-abc"

    def test_it_does_not_look_like_a_bare_id(self):
        cap = cu.mint("plan-log", "run-abc")
        assert cu.looks_like_capability(cap)
        assert not cu.looks_like_capability("run-abc")
        assert "run-abc" not in cap.split(".")[1]  # the signature is not the id


class TestItCannotBeForgedOrReplayed:
    def test_a_tampered_payload_is_refused(self):
        cap = cu.mint("plan-log", "run-abc")
        body, _, sig = cap[len("cap-") :].partition(".")
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        forged = payload.replace("run-abc", "run-xyz")
        b2 = base64.urlsafe_b64encode(forged.encode()).decode().rstrip("=")
        assert cu.verify(f"cap-{b2}.{sig}", expect_kind="plan-log") is None

    def test_a_capability_for_another_kind_is_refused(self):
        # Both log endpoints resolve the same run id, so without the kind check
        # a plan capability would read the apply log.
        cap = cu.mint("plan-log", "run-abc")
        assert cu.verify(cap, expect_kind="apply-log") is None

    def test_an_expired_capability_is_refused(self):
        cap = cu.mint("plan-log", "run-abc", ttl_seconds=1)
        with patch.object(cu.time, "time", return_value=time.time() + 10):
            assert cu.verify(cap, expect_kind="plan-log") is None

    def test_rubbish_is_refused(self):
        for s in ("run-abc", "cap-", "cap-x", "cap-!!!.sig", "", "cap-YWJj.wrongsig"):
            assert cu.verify(s, expect_kind="plan-log") is None


class TestTheKeyIsNotTheWeakOne:
    def test_the_weak_key_has_no_influence_on_the_signature(self):
        """The whole point, asserted behaviourally.

        `download_tickets` signs with `get_token_signing_key()`, which defaults
        to sha256(database_url) — the weakness hc47 reports, and one we do not
        rotate on the release lines. If capabilities depended on it, an attacker
        who knew the DSN could mint their own. Changing it must change nothing.
        """
        cap = cu.mint("plan-log", "run-abc")
        with patch("terrapod.config.settings") as st:
            st.token_signing_key = "a-completely-different-secret"
            st.database_url = "postgresql+asyncpg://someone:else@elsewhere/db"
            assert cu.verify(cap, expect_kind="plan-log") == "run-abc"

    def test_a_different_ca_yields_a_different_signature(self):
        cap_a = cu.mint("plan-log", "run-abc")
        other = _ca_with_key(bytes(range(1, 33)))
        with patch("terrapod.auth.ca.get_ca", return_value=other):
            assert cu.verify(cap_a, expect_kind="plan-log") is None


class TestAuditSafety:
    """A capability must never be written into a table auditors read."""

    def test_it_describes_what_the_capability_names_not_the_capability(self):
        cap = cu.mint(cu.KIND_PLAN_LOG, "run-abc")
        described = cu.describe_for_logging(cap)
        assert described == "plan-log:run-abc"
        # The point of the exercise: the secret itself is gone.
        assert cap not in described
        assert cap.split(".")[1] not in described

    def test_a_bare_id_is_left_alone(self):
        assert cu.describe_for_logging("run-abc") is None

    def test_an_unparseable_capability_still_yields_no_secret(self):
        # A forged or truncated capability must not fall through to being
        # recorded verbatim.
        described = cu.describe_for_logging("cap-!!!!.sig")
        assert described == "capability:unparseable"

    def test_describing_does_not_authorise(self):
        # describe_for_logging decodes without verifying, by design. The
        # guard is that it cannot be mistaken for an id: it never returns
        # one, so a call site that wrongly used it would not resolve.
        forged = cu.mint(cu.KIND_PLAN_LOG, "run-abc")
        body, _, _ = forged[len("cap-") :].partition(".")
        tampered = f"cap-{body}.not-a-signature"
        assert cu.describe_for_logging(tampered) == "plan-log:run-abc"
        assert cu.verify(tampered, expect_kind=cu.KIND_PLAN_LOG) is None
