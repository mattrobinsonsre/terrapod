"""What must never replicate, and why (#1138, #1143).

Two classes are deliberately outside the replication scope. They share one
principle: **private keys do not travel.** The peer link is authenticated and
values are re-encrypted under the receiver's own key, but the right amount of key
material to put on a link that does not need it is none.

This is the single place that rule is enforced, so a future "just replicate it
too, it would be simpler" has somewhere to fail.
"""

from terrapod.db.models import CertificateAuthorityModel, CryptoKey
from terrapod.services import replication


class TestTheEncryptionKeysNeverTravel:
    """`crypto_keys` holds this node's data-encryption key wrapped by THIS node's
    KEK. Sending it is useless to the peer — it cannot unwrap it — and per-node
    encryption exists precisely so the key never has to travel: values are
    decrypted on send and re-encrypted under the receiver's own key.
    """

    def test_crypto_keys_is_not_registered(self):
        assert "crypto_keys" not in replication.registered()

    def test_no_registered_class_is_the_crypto_key_model(self):
        """Catches a registration under a different class name."""
        assert CryptoKey not in {spec.model for spec in replication.registered().values()}


class TestTheCertificateAuthorityStaysNodeLocal:
    """Each node generates and keeps its own CA (#1143).

    The reasoning, because this looked like a gap and is in fact a decision:

    **Nothing durable is signed by it.** The CA issues listener certificates and
    nothing else, and those are short-lived — `listener_cert_ttl_seconds`, renewed
    at half their validity. No artifact's long-term verifiability depends on the
    pair sharing a CA.

    **The failover path does not need it.** A shared listener fleet re-points at
    the promoted node, its certificates stop authenticating, and each listener
    re-joins with the token it already holds — which works because join *tokens*
    replicate. The trust chain is rebuilt at the failover rather than carried
    through it, by design.

    **So replicating would be strictly worse:** a CA private key on the peer link
    and resident on both nodes, buying nothing the re-join path does not already
    deliver, and contradicting the stance taken for `crypto_keys` directly above.

    It would also need real coordination work to be safe at all — `init_ca`
    generates-if-absent on every node and the table has no unique constraint, so
    a naive registration leaves two rows and a non-deterministic choice between
    them. Cost, for no benefit.

    The one real consequence, stated honestly: every listener re-joins at a
    failover, spending one join-token use each. That is exactly why the docs tell
    operators to size join tokens for the shared-fleet topology as long-lived and
    generously reusable.
    """

    def test_the_certificate_authority_is_not_registered(self):
        assert "certificate_authority" not in replication.registered(), (
            "The CA is node-local by decision (#1143), not by omission. It signs "
            "only short-lived listener certificates, and a failover rebuilds the "
            "trust chain by re-joining — so replicating it would put a CA private "
            "key on the peer link for no benefit."
        )

    def test_no_registered_class_is_the_ca_model(self):
        assert CertificateAuthorityModel not in {
            spec.model for spec in replication.registered().values()
        }

    def test_the_ca_only_ever_issues_short_lived_certificates(self):
        """The load-bearing premise of the decision. If the CA ever started
        signing something durable — a release artifact, a long-lived token — the
        reasoning above would no longer hold and #1143 would need reopening.

        Asserted by source inspection rather than trusted: `issue_listener_certificate`
        takes a TTL, and it is the only issuing entry point on the CA.
        """
        import inspect as py_inspect

        from terrapod.auth import ca as ca_module

        issuers = [
            name
            for name, _ in py_inspect.getmembers(ca_module.CertificateAuthority)
            if name.startswith("issue")
        ]

        assert issuers == ["issue_listener_certificate"], (
            f"The CA gained a new issuing method ({issuers}). If it now signs "
            "anything durable, reopen #1143 — the 'node-local is fine' argument "
            "rests on everything it signs being short-lived."
        )

        signature = py_inspect.signature(ca_module.CertificateAuthority.issue_listener_certificate)
        assert "ttl_seconds" in signature.parameters
