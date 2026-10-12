"""The OIDC issuer signing key over its whole life, against real rows.

Three things need a real Postgres engine rather than a mock, and each one of
them has already been got wrong once:

* **The init race.** `init_oidc_signing` uses a transaction-scoped advisory
  lock, which is a Postgres feature. The failure it prevents is the #1060 CA
  race in a worse place: two replicas each see an empty table, each generate a
  keypair, each insert — and the deployment then publishes a JWKS that depends
  on which replica answered, so a cloud fetches one key and a token is signed
  with the other. Federation fails intermittently and for no visible reason.

* **The propagation window.** A rotation's correctness is entirely about the
  real timestamps on real rows: the incoming key must be *published* at once and
  must not *sign* until the clouds have had a chance to fetch it. The first
  implementation of `_choose_signing_kid` got this backwards — it looked only for
  an unretired, already-active key, which during a rotation is empty by
  construction, and fell through to signing with the brand-new key. Every
  federated workspace would have failed for the length of the window, and
  `key_propagation_seconds` was dead configuration. There were no tests for it.

* **The grace window.** A retired key keeps verifying until the tokens signed
  with it expire, then must leave the published set. That is clock arithmetic
  over stored columns.

The unit tier covers `_choose_signing_kid`'s tiers directly. This file drives the
real functions against the real table, so the columns, the defaults and the
lock are all in play.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import text

from terrapod.auth import oidc_signing
from terrapod.auth.oidc_signing import (
    get_jwks,
    get_signing_key,
    init_oidc_signing,
    reload_signing_keys,
    rotate_signing_key,
)
from terrapod.config import settings
from terrapod.db.session import get_db_session


async def _clean_slate() -> None:
    async with get_db_session() as session:
        await session.execute(text("DELETE FROM oidc_signing_keys"))
        await session.commit()
    oidc_signing._reset_for_tests()


async def _init_once() -> str:
    """Run init on its own session/connection and return the signing kid."""
    async with get_db_session() as session:
        await init_oidc_signing(session)
    return get_signing_key().kid


async def _row_count() -> int:
    async with get_db_session() as session:
        return (await session.execute(text("SELECT count(*) FROM oidc_signing_keys"))).scalar_one()


def _kids(jwks: dict) -> set[str]:
    return {entry["kid"] for entry in jwks["keys"]}


async def _unretired_kids() -> set[str]:
    """Read from the ROW, not from the published set.

    A key with no `retired_at` is one no grace window can ever expire, and that
    is invisible in the JWKS -- which shows it exactly as it shows a key retired
    one second ago. The defect this exists for lived in that gap.
    """
    async with get_db_session() as session:
        rows = await session.execute(
            text("SELECT kid FROM oidc_signing_keys WHERE retired_at IS NULL")
        )
        return {r[0] for r in rows}


class TestTheInitRace:
    async def test_concurrent_init_creates_exactly_one_key(self, app):
        """Four replicas starting together must agree on one trust root.

        Without the advisory lock each inserts its own row, and which key the
        JWKS advertises then depends on which replica serves the request.
        """
        await _clean_slate()

        kids = await asyncio.gather(*[_init_once() for _ in range(4)])

        assert await _row_count() == 1, (
            f"expected one signing key row, found {await _row_count()} — concurrent "
            "replicas each created their own trust root"
        )
        assert len(set(kids)) == 1, f"replicas disagree on the signing key: {set(kids)}"

    async def test_init_is_idempotent_once_a_key_exists(self, app):
        await _clean_slate()

        first = await _init_once()
        second = await _init_once()

        assert await _row_count() == 1
        assert first == second

    async def test_the_kid_is_derived_from_the_key_not_random(self, app):
        """A restart must not change the kid of a key it did not change.

        `kid` is the RFC 7638 thumbprint of the public key, so it is a function
        of the key itself. A random kid would make every restart look like a
        rotation to anything caching the JWKS by kid.
        """
        await _clean_slate()
        kid = await _init_once()

        async with get_db_session() as session:
            pem = (
                await session.execute(text("SELECT private_key_pem FROM oidc_signing_keys"))
            ).scalar_one()

        # Recomputed from the stored key, independently of the cache.
        recomputed = oidc_signing.compute_kid(oidc_signing.load_private_key(pem))
        assert recomputed == kid


class TestRotation:
    async def test_a_rotation_publishes_the_new_key_but_keeps_signing_with_the_old(self, app):
        """The regression test for the propagation-window defect.

        At the instant of a rotation the incoming key is published and not yet
        active, and the outgoing key is retired. So the set of
        "unretired AND active" keys is EMPTY — which is exactly the state the
        first implementation mishandled by signing with the new key, which no
        cloud had fetched yet.
        """
        await _clean_slate()
        old_kid = await _init_once()

        async with get_db_session() as session:
            new = await rotate_signing_key(session)

        assert new.kid != old_kid
        assert await _row_count() == 2, "a rotation adds a key rather than replacing one"

        # Published: both, so a cloud fetching now can verify tokens signed
        # either side of the rotation.
        assert _kids(get_jwks()) == {old_kid, new.kid}

        # Signing: still the old one. This is the assertion that fails if the
        # propagation window is ever bypassed again.
        assert get_signing_key().kid == old_kid, (
            "signing with the brand-new key during its propagation window — no "
            "cloud has had a chance to fetch it, so every federated workspace "
            "fails until they do. key_propagation_seconds exists to prevent this."
        )

    async def test_the_new_key_takes_over_once_its_window_has_passed(self, app):
        """And the window is a window, not a permanent freeze.

        Tested by moving `activates_at` into the past -- the honest way to
        exercise a time-based transition without sleeping through it.
        """
        await _clean_slate()
        old_kid = await _init_once()

        async with get_db_session() as session:
            new = await rotate_signing_key(session)

        async with get_db_session() as session:
            await session.execute(
                text("UPDATE oidc_signing_keys SET activates_at = :t WHERE kid = :kid"),
                {"t": datetime.now(UTC) - timedelta(seconds=1), "kid": new.kid},
            )
            await session.commit()
            await reload_signing_keys(session)

        assert get_signing_key().kid == new.kid, (
            "the new key never takes over, so a rotation would leave the "
            "deployment signing with a retired key for ever"
        )
        # The old one is still published until its grace window expires.
        assert old_kid in _kids(get_jwks())

    async def test_a_key_retired_past_its_grace_window_leaves_the_published_set(self, app):
        """Published forever would mean a stolen old key verifies forever."""
        await _clean_slate()
        old_kid = await _init_once()

        async with get_db_session() as session:
            new = await rotate_signing_key(session)

        grace = settings.auth.oidc_issuer.retired_key_grace_seconds
        async with get_db_session() as session:
            # Activate the incoming key, and push the outgoing one's retirement
            # back beyond the grace window.
            await session.execute(
                text("UPDATE oidc_signing_keys SET activates_at = :t WHERE kid = :kid"),
                {"t": datetime.now(UTC) - timedelta(seconds=1), "kid": new.kid},
            )
            await session.execute(
                text("UPDATE oidc_signing_keys SET retired_at = :t WHERE kid = :kid"),
                {"t": datetime.now(UTC) - timedelta(seconds=grace + 60), "kid": old_kid},
            )
            await session.commit()
            await reload_signing_keys(session)

        assert _kids(get_jwks()) == {new.kid}, (
            "a key retired well past its grace window is still published, so "
            "tokens signed with it keep verifying indefinitely"
        )
        assert get_signing_key().kid == new.kid

    async def test_a_SECOND_rotation_inside_the_window_orphans_nothing(self, app):
        """The regression test for the orphaned-key defect.

        Rotation used to retire only the key *currently signing*. During a
        propagation window the signer is the PREVIOUS key, already retired
        (tier 2 of `_choose_signing_kid`) -- so a second rotation in that window
        found `retired_at` already set, retired nothing, and left the first
        rotation's pending key unretired for ever. No `retired_at` means no
        grace window to measure, so it stayed published until it eventually
        began signing and some later rotation happened to catch it.

        Rotating twice in quick succession is not an odd thing to do: it is what
        an operator does during a key-compromise incident, which is precisely
        when they believe a key has been revoked and need that to be true.

        The property is stated as an invariant rather than as a count of this
        scenario: **after any rotation exactly one key is unretired, and it is
        the new one.**
        """
        await _clean_slate()
        first_kid = await _init_once()

        async with get_db_session() as session:
            second = await rotate_signing_key(session)

        # Mid-window: the incoming key is not yet active, so the retired first
        # key is what signs. This is the state the defect needed.
        assert get_signing_key().kid == first_kid

        async with get_db_session() as session:
            third = await rotate_signing_key(session)

        assert await _row_count() == 3, "each rotation adds a key"
        assert await _unretired_kids() == {third.kid}, (
            "a key is published with no retirement date after a second rotation "
            "inside the propagation window, so nothing can ever expire it -- "
            "during a compromise the operator believes it is revoked and it is "
            "still a live trust root"
        )
        # And the two retired ones are still published, because tokens minted
        # before each rotation have to keep verifying for their grace window.
        assert _kids(get_jwks()) == {first_kid, second.kid, third.kid}

    async def test_two_replicas_rotating_at_once_still_leave_one_unretired_key(self, app):
        """Rotation takes the same advisory lock as init, for the same reason.

        Unserialised, both replicas read "the unretired set is {A}", both retire
        A, and both insert their own key -- leaving TWO unretired keys and no
        answer to "which one is current". That is the orphan defect arriving by
        a second route, and it needs no propagation window to reach: two
        operators, or one operator and a retry, are enough.

        The lock makes the loser's read happen after the winner's write, so it
        retires the winner's key and the invariant holds: exactly one unretired
        key, and it is the newest.
        """
        await _clean_slate()
        await _init_once()

        async def _rotate() -> str:
            async with get_db_session() as session:
                return (await rotate_signing_key(session)).kid

        kids = await asyncio.gather(*[_rotate() for _ in range(2)])

        assert await _row_count() == 3, "each rotation adds exactly one key"
        unretired = await _unretired_kids()
        assert len(unretired) == 1, (
            f"two concurrent rotations left {len(unretired)} unretired keys "
            f"({unretired}) -- each one a trust root nothing can expire"
        )
        assert unretired <= set(kids)

    async def test_the_emergency_zero_window_rotation_unpublishes_at_once(self, app):
        """`key_propagation_seconds=0` with `retired_key_grace_seconds=0`.

        This is the configuration the compromise runbook tells an operator to
        set: sign with the new key now, stop publishing the old one now. Both
        values accept 0 deliberately -- which is also why the chart renders them
        with no `| default`, since a Go template treats 0 as empty and would
        silently restore the ordinary windows at the worst possible moment.
        """
        await _clean_slate()
        old_kid = await _init_once()

        # Patched on the INSTANCE, not the class: these are Pydantic fields, so
        # the class carries no such attribute and `patch.object(type(cfg), ...)`
        # raises. Everything that reads them goes through this one singleton.
        cfg = settings.auth.oidc_issuer
        with (
            patch.object(cfg, "key_propagation_seconds", 0),
            patch.object(cfg, "retired_key_grace_seconds", 0),
        ):
            async with get_db_session() as session:
                new = await rotate_signing_key(session)

            assert _kids(get_jwks()) == {new.kid}, (
                f"{old_kid} is still published after an emergency rotation, so "
                "every cloud goes on trusting the compromised key"
            )
            assert get_signing_key().kid == new.kid

    async def test_a_rotation_reaches_another_replica_without_a_restart(self, app):
        """One replica rotates; another picks it up by re-reading the table.

        This is what the periodic reload is for. Simulated by clearing the
        process cache -- the state a replica that did not perform the rotation
        is in -- and reloading from the database alone.
        """
        await _clean_slate()
        old_kid = await _init_once()

        async with get_db_session() as session:
            new = await rotate_signing_key(session)

        # A replica that knows nothing of the rotation.
        oidc_signing._reset_for_tests()
        async with get_db_session() as session:
            await reload_signing_keys(session)

        assert _kids(get_jwks()) == {old_kid, new.kid}
        assert get_signing_key().kid == old_kid, (
            "the other replica disagrees about which key signs, so which key a "
            "token is signed with depends on which replica served the run"
        )


class TestSigning:
    async def test_a_signed_token_verifies_against_the_published_jwks(self, app):
        """End to end: what we publish is what a cloud would need.

        The claim worth testing at this tier is not the JWT library but that the
        `kid` in the header resolves inside the published set -- a cloud that
        cannot find the kid rejects the token without looking at it.
        """
        import jwt
        from jwt import PyJWK

        await _clean_slate()
        await _init_once()

        token = oidc_signing.sign_identity_token(
            {"iss": "https://example.test", "sub": "workspace:demo", "aud": "sts.example"},
            ttl_seconds=300,
        )
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        assert header["kid"] in _kids(get_jwks())

        entry = next(e for e in get_jwks()["keys"] if e["kid"] == header["kid"])
        claims = jwt.decode(
            token,
            key=PyJWK.from_dict(entry).key,
            algorithms=["RS256"],
            audience="sts.example",
        )
        assert claims["sub"] == "workspace:demo"
        assert claims["exp"] > claims["iat"]
        # `jti` is what distinguishes two tokens for the same run in a cloud
        # audit log, so an absent one is a real gap rather than a nicety.
        assert uuid.UUID(claims["jti"])
