"""Replication of the OIDC issuer signing keys (#1901).

This is the class that decides whether federated runs work after a failover, and
it fails silently. `init_oidc_signing` generates a key when the table is empty
and `activates_at` defaults to now, so a promoted node with no rows mints a fresh
keypair and signs with it immediately -- logged at INFO, indistinguishable from
ordinary startup. The issuer URL has not changed, so every cloud still trusts the
issuer while the JWKS it cached holds only the old `kid`: every
AssumeRoleWithWebIdentity, Entra federated credential, GCP WIF exchange and Vault
JWT login fails, and nothing recovers until each cloud's cache expires.

The rotation machinery cannot cover it. Tier 2 of `_choose_signing_kid` exists for
exactly that window -- keep signing with the retired key the clouds can still
verify -- and at a failover there is no old row at all, so the tier is empty by
construction.

**The three timestamps are the load-bearing part, not the key bytes.** A node that
carries every keypair but loses `activates_at` makes a key that was published-but-
not-yet-signing immediately active, which is the propagation guarantee inverted;
one that loses `retired_at` keeps signing with a key it has already stood down, or
republishes one whose grace has expired. So they get their own assertions rather
than being left implied by "the columns carry".
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.crypto.types import EncryptedText
from terrapod.db.models import OIDCSigningKey
from terrapod.services import replication, replication_registry

KEYS = replication_registry.OIDC_SIGNING_KEYS

KEY_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"

# Shaped like a real RFC 7638 thumbprint: that is what a cloud's cached JWKS
# names, so it is the value whose loss causes the outage above.
KID = "x9Qf2LdR7vK0mTnB4wHsYpAeJcUgZr1oNiXvQ3bElSk"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n"


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _ids_db(ids):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = ids
    db.execute.return_value = result
    return db


def _key(key_id=KEY_ID, **kw):
    now = datetime.now(UTC)
    base = {
        "id": key_id,
        "kid": KID,
        "private_key_pem": PEM,
        "created_at": now,
        "activates_at": now,
        "retired_at": None,
    }
    base.update(kw)
    return OIDCSigningKey(**base)


class TestTheSigningKeyItselfTravels:
    """The decision this class exists to make, and the one a reader will query.

    `gpg_keys` excludes its private key because the receiving node has no use for
    it -- the server never signs. Here the receiving node MUST sign, so excluding
    it would leave the follower publishing a JWKS advertising a trust root it
    cannot sign for: worse than not replicating at all.
    """

    def test_the_private_key_is_not_excluded(self):
        assert "private_key_pem" not in KEYS.exclude, (
            "the follower must be able to SIGN, not merely publish the kid -- "
            "excluding the key leaves it advertising a trust root it cannot use"
        )

    @pytest.mark.replication_matrix("oidc_signing_keys", "encrypted-columns")
    def test_the_key_is_encrypted_at_rest_on_both_sides(self):
        from sqlalchemy import inspect as sa_inspect

        encrypted = {
            col.key
            for col in sa_inspect(OIDCSigningKey).column_attrs
            if isinstance(col.expression.type, EncryptedText)
        }
        assert encrypted == {"private_key_pem"}

    async def test_applying_writes_back_through_the_encrypted_column(self):
        """So the receiver re-encrypts under its own key.

        This is why the row can travel at all while `crypto_keys` cannot: the KEK
        stays node-local and the value is decrypted on send, re-encrypted on
        arrival -- the same per-node path `variables.value` already uses.
        """
        db = AsyncMock()
        existing = _key(private_key_pem="stale")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, KEYS, {"id": KEY_ID, "private_key_pem": PEM})

        assert existing.private_key_pem == PEM


class TestOIDCSigningKeys:
    @pytest.mark.replication_matrix("oidc_signing_keys", "backfill-from-empty")
    async def test_backfill_carries_the_kid_the_key_and_all_three_timestamps(self):
        """A promoted node needs every live key, not just the signing one: the
        JWKS is a set, and a token signed before the failover verifies against
        whichever key signed it."""
        activates = datetime.now(UTC) - timedelta(hours=2)
        retired = datetime.now(UTC) - timedelta(minutes=5)
        db = _rows_db([_key(activates_at=activates, retired_at=retired)])

        page = await replication.read_backfill(db, KEYS)

        assert page[0]["kid"] == KID
        assert page[0]["private_key_pem"] == PEM
        # The timestamps decide which key signs and which is still published,
        # so assert the INSTANT survives rather than that a field is present.
        # They travel as RFC3339 with a trailing Z (the house rule), which is
        # also what the far end parses.
        for field, expected in (("activates_at", activates), ("retired_at", retired)):
            raw = page[0][field]
            assert raw.endswith("Z"), f"{field} must be RFC3339 with Z, got {raw}"
            assert datetime.fromisoformat(raw.replace("Z", "+00:00")) == expected, field
        assert page[0]["created_at"].endswith("Z")

    @pytest.mark.replication_matrix("oidc_signing_keys", "delta-apply")
    async def test_a_rotation_reaches_the_peer_as_a_retirement(self):
        """A rotation is two deltas: the incoming key arrives as a new row, and
        the outgoing one gains `retired_at`. This is the second, and it is the one
        that moves which key signs -- so a peer that applies the insert and misses
        the retirement goes on signing with a key the leader has stood down."""
        db = AsyncMock()
        existing = _key(retired_at=None)
        db.scalar.return_value = existing
        retired = datetime.now(UTC)

        await replication.apply_upsert(db, KEYS, {"id": KEY_ID, "retired_at": retired})

        assert existing.retired_at == retired

    @pytest.mark.replication_matrix("oidc_signing_keys", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _key()
        db.scalar.return_value = existing
        payload = replication.serialize_row(KEYS, existing)

        await replication.apply_upsert(db, KEYS, payload)
        await replication.apply_upsert(db, KEYS, payload)

        assert existing.kid == KID
        assert existing.private_key_pem == PEM
        assert existing.retired_at is None

    @pytest.mark.replication_matrix("oidc_signing_keys", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, KEYS, KEY_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("oidc_signing_keys", "backfill-converges-deletion")
    async def test_a_purged_key_does_not_come_back_to_life(self):
        """A key whose grace has expired is deliberately gone. Resurrecting it
        republishes it in the JWKS, re-advertising a trust root the operator
        removed -- the #1115 defect, on the one row that is a published trust
        anchor rather than a record."""
        db = _ids_db([(KEY_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, KEYS, {KEY_ID})

        assert removed == [OTHER_ID]
