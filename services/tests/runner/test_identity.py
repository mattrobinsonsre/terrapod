"""Tests for listener identity — Secret-backed bootstrap, splay, /renew retries."""

import base64
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.config import RunnerConfig
from terrapod.runner import identity as identity_mod

# establish_identity()/rejoin_identity() now take their non-sensitive
# inputs from RunnerConfig (runners.yaml + env layering) rather than reading
# os.environ directly (#617). These tests construct a RunnerConfig with the
# same name/API URL the env used to provide.
_RC = RunnerConfig(listener_name="core", server_url="http://api")

# ── pod_splay_seconds ───────────────────────────────────────────────


class TestPodSplaySeconds:
    def test_deterministic_for_same_pod_name(self):
        a = identity_mod.pod_splay_seconds("listener-7f8b9c6d4-x2k3m")
        b = identity_mod.pod_splay_seconds("listener-7f8b9c6d4-x2k3m")
        assert a == b

    def test_within_default_max(self):
        v = identity_mod.pod_splay_seconds("listener-7f8b9c6d4-x2k3m")
        assert 0 <= v < 30

    def test_respects_custom_max(self):
        v = identity_mod.pod_splay_seconds("listener-7f8b9c6d4-x2k3m", max_splay=10)
        assert 0 <= v < 10

    def test_different_pods_get_different_values_typically(self):
        # Not strictly guaranteed (birthday paradox), but with 100 names
        # the spread should cover most of 0..29.
        names = [f"listener-{i}-x" for i in range(100)]
        values = {identity_mod.pod_splay_seconds(n) for n in names}
        assert len(values) >= 15  # at least half the bucket space

    def test_falls_back_to_zero_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POD_NAME", None)
            os.environ.pop("HOSTNAME", None)
            assert identity_mod.pod_splay_seconds("") == 0


# ── _call_renew_with_retries ────────────────────────────────────────


def _identity():
    return identity_mod.ListenerIdentity(
        listener_id=uuid.uuid4(),
        name="listener",
        pool_id=uuid.uuid4(),
        api_url="http://test",
        certificate_pem="-----BEGIN CERT-----\nfoo\n-----END CERT-----\n",
        private_key_pem=_TEST_KEY_PEM,
        ca_cert_pem="ca",
    )


def _make_key_pem() -> str:
    """A real key, so the renewal tests exercise the signing path rather than
    skipping it. A placeholder string made sign_request raise, which is how the
    unusable-key case below went unnoticed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


_TEST_KEY_PEM = _make_key_pem()


class TestCallRenewWithRetries:
    @pytest.mark.asyncio
    async def test_returns_data_on_200(self):
        ident = _identity()
        new_data = {"certificate": "PEM", "private_key": "K", "ca_certificate": "CA"}

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": new_data}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await identity_mod._call_renew_with_retries(ident)

        assert result == new_data

    @pytest.mark.asyncio
    async def test_raises_cert_rejected_on_401(self):
        """401 means the cert is refused outright — raise, do not return None.

        This used to return None, which is also what a transient failure
        returned, so the caller could not tell "retry later" from "this cert
        will never work again" and retried a rejected cert for ever. The part
        of the original reasoning that still holds is below: no retries.
        """
        ident = _identity()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "expired"

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(identity_mod._CertRejected):
                await identity_mod._call_renew_with_retries(ident)

        # Single call — no retries on auth failure
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self):
        ident = _identity()

        mock_500 = MagicMock(status_code=500, text="boom")
        mock_200 = MagicMock(status_code=200)
        mock_200.json.return_value = {"data": {"certificate": "PEM"}}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = [mock_500, mock_200]

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await identity_mod._call_renew_with_retries(ident)

        assert result == {"certificate": "PEM"}
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_after_three_failures(self):
        ident = _identity()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = httpx.ConnectError("network down")

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await identity_mod._call_renew_with_retries(ident)

        assert result is None
        assert mock_client.post.call_count == 3


# ── establish_identity (bootstrap) ──────────────────────────────────


def _v1secret(*, listener_id, pool_id, cert="PEM", key="K", ca="CA", rv="123"):
    """Build a fake V1Secret matching the structure read_secret() returns."""
    secret = MagicMock()
    secret.metadata = MagicMock()
    secret.metadata.resource_version = rv
    secret.data = {
        identity_mod._K_TLS_CRT: base64.b64encode(cert.encode()).decode(),
        identity_mod._K_TLS_KEY: base64.b64encode(key.encode()).decode(),
        identity_mod._K_CA_CRT: base64.b64encode(ca.encode()).decode(),
        identity_mod._K_LISTENER_ID: base64.b64encode(str(listener_id).encode()).decode(),
        identity_mod._K_POOL_ID: base64.b64encode(str(pool_id).encode()).decode(),
    }
    return secret


class TestEstablishIdentity:
    @pytest.mark.asyncio
    async def test_resumes_from_existing_secret(self):
        """Secret exists → return its identity without ever hitting /join."""
        lid = uuid.uuid4()
        pid = uuid.uuid4()
        secret = _v1secret(listener_id=lid, pool_id=pid)

        env = {"TERRAPOD_LISTENER_NAME": "core", "TERRAPOD_API_URL": "http://api"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=secret),
            patch.object(identity_mod, "_call_join", new=AsyncMock()) as mock_join,
        ):
            result = await identity_mod.establish_identity(_RC)

        assert result.listener_id == lid
        assert result.pool_id == pid
        assert result.name == "core"
        # /join was never called — Secret was the source of truth
        mock_join.assert_not_called()

    @pytest.mark.asyncio
    async def test_bootstraps_via_join_token_when_secret_absent(self):
        """No Secret → /join → create Secret → return identity."""
        lid = str(uuid.uuid4())
        pid = str(uuid.uuid4())
        join_response = {
            "listener_id": lid,
            "pool_id": pid,
            "certificate": "PEM",
            "private_key": "K",
            "ca_certificate": "CA",
        }
        created_secret = _v1secret(listener_id=lid, pool_id=pid, rv="42")

        env = {
            "TERRAPOD_LISTENER_NAME": "core",
            "TERRAPOD_API_URL": "http://api",
            "TERRAPOD_JOIN_TOKEN": "the-token",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=None),
            patch.object(identity_mod, "_call_join", new=AsyncMock(return_value=join_response)),
            patch.object(identity_mod, "_create_secret", return_value=created_secret),
        ):
            result = await identity_mod.establish_identity(_RC)

        assert str(result.listener_id) == lid
        assert str(result.pool_id) == pid
        assert result.secret_resource_version == "42"

    @pytest.mark.asyncio
    async def test_lost_create_race_adopts_winners_secret(self):
        """Two pods: ours wins /join, theirs wins create → we adopt their cert."""
        winner_lid = uuid.uuid4()
        winner_pid = uuid.uuid4()
        loser_join_response = {
            "listener_id": str(uuid.uuid4()),  # we got our own listener-id from join
            "pool_id": str(winner_pid),
            "certificate": "LOSER_PEM",
            "private_key": "LOSER_K",
            "ca_certificate": "CA",
        }
        winner_secret = _v1secret(listener_id=winner_lid, pool_id=winner_pid, cert="WINNER_PEM")

        # _read_secret returns None first (pre-join check), then the winner's secret
        # (post-create-race re-read).
        read_calls = iter([None, winner_secret])

        env = {
            "TERRAPOD_LISTENER_NAME": "core",
            "TERRAPOD_API_URL": "http://api",
            "TERRAPOD_JOIN_TOKEN": "tok",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(
                identity_mod, "_read_secret", side_effect=lambda *_a, **_k: next(read_calls)
            ),
            patch.object(
                identity_mod, "_call_join", new=AsyncMock(return_value=loser_join_response)
            ),
            patch.object(
                identity_mod,
                "_create_secret",
                side_effect=identity_mod._SecretAlreadyExists("409"),
            ),
        ):
            result = await identity_mod.establish_identity(_RC)

        # We adopted the winner's cert, not the one /join handed us
        assert result.listener_id == winner_lid
        assert result.certificate_pem == "WINNER_PEM"

    @pytest.mark.asyncio
    async def test_join_token_exhausted_then_secret_appears(self):
        """Lost the /join race entirely (max_uses exhausted) → poll Secret."""
        winner_lid = uuid.uuid4()
        winner_pid = uuid.uuid4()
        winner_secret = _v1secret(listener_id=winner_lid, pool_id=winner_pid)

        # Start: no Secret, /join returns 401. Next attempt: Secret has appeared.
        read_returns = iter([None, winner_secret])

        env = {
            "TERRAPOD_LISTENER_NAME": "core",
            "TERRAPOD_API_URL": "http://api",
            "TERRAPOD_JOIN_TOKEN": "tok",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(
                identity_mod, "_read_secret", side_effect=lambda *_a, **_k: next(read_returns)
            ),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(side_effect=identity_mod._JoinTokenExhausted("401")),
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await identity_mod.establish_identity(_RC)

        assert result.listener_id == winner_lid

    @pytest.mark.asyncio
    async def test_no_join_token_and_no_secret_raises(self):
        """Cold start with neither Secret nor join token → fail loudly."""
        env = {"TERRAPOD_LISTENER_NAME": "core"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="TERRAPOD_JOIN_TOKEN"):
                await identity_mod.establish_identity(_RC)


class TestAnUnusableKeyDoesNotCrashRenewal:
    """A corrupt stored key must leave the join-token fallback reachable.

    The caller recovers from a refused renewal by re-joining, which issues a
    fresh key and is exactly the right recovery for a corrupt one. If signing
    raised out of here instead, that self-healing case would become a crash
    loop — so signing is best-effort and an unsigned request is sent, which an
    API requiring proof of possession refuses with the 401 that drives the
    re-join.

    The refusal is now `_CertRejected` rather than a None return. The part of
    the original reasoning that still holds is the part being tested: signing
    must not be what raises out of here.
    """

    @pytest.mark.asyncio
    async def test_a_malformed_key_sends_unsigned_rather_than_raising(self):
        ident = _identity()
        ident.private_key_pem = "not a pem at all"

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "no"
        client = MagicMock()
        client.post = AsyncMock(return_value=mock_response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=cm):
            with pytest.raises(identity_mod._CertRejected):
                await identity_mod._call_renew_with_retries(ident)

        sent = client.post.await_args.kwargs["headers"]
        assert "X-Terrapod-Client-Cert" in sent
        assert "X-Terrapod-Listener-Signature" not in sent, (
            "a key that will not load must not leave a half-built signature behind"
        )


# ── renew_loop: what happens when the API refuses the cert ──────────


async def _drive_renew_loop(*, on_cert_rejected, second_cycle_error):
    """Run one `renew_loop` cycle that is refused, then break out on the next.

    The loop is a `while True`, so the exit is an unexpected exception from the
    second `/renew` — which also proves the loop came back round rather than
    sleeping on the rejection for ever.
    """
    holder = SimpleNamespace(identity=_identity())
    calls = {"n": 0}

    async def fake_renew(cert):
        calls["n"] += 1
        if calls["n"] == 1:
            raise identity_mod._CertRejected("renew returned 401")
        raise second_cycle_error

    sleeper = AsyncMock()
    with (
        patch.object(identity_mod, "_call_renew_with_retries", side_effect=fake_renew),
        patch.object(identity_mod, "_read_secret", return_value=None),
        # An already-expired cert, which is the case that cannot renew itself.
        patch.object(identity_mod, "cert_not_after", return_value=datetime.now(UTC)),
        patch("asyncio.sleep", sleeper),
    ):
        with pytest.raises(type(second_cycle_error), match="second cycle"):
            await identity_mod.renew_loop(
                holder,
                secret_name="s",
                namespace="n",
                cert_validity_seconds=300,
                splay_seconds=0,
                on_cert_rejected=on_cert_rejected,
            )
    return calls, sleeper


class TestTheRenewLoopReportsARefusedCert:
    @pytest.mark.asyncio
    async def test_a_rejection_is_reported_and_the_loop_retries_at_once(self):
        """A reported rejection that led to a re-join must not also sleep.

        The listener has replaced its identity by then, so the next cycle has
        something new to try. Sleeping first is what left a listener wedged for
        25 minutes with an expired cert it could never renew.
        """
        reported = []

        async def on_rejected():
            reported.append(True)
            return True  # a re-join happened

        calls, sleeper = await _drive_renew_loop(
            on_cert_rejected=on_rejected,
            second_cycle_error=RuntimeError("second cycle"),
        )
        assert reported == [True], "the refusal must reach the listener"
        assert calls["n"] == 2, "the loop must come back round, not sleep"
        assert sleeper.await_count == 0

    @pytest.mark.asyncio
    async def test_with_no_reporter_wired_it_sleeps_and_retries(self):
        """`renew_loop` stays usable on its own, so the callback is optional."""
        calls, sleeper = await _drive_renew_loop(
            on_cert_rejected=None,
            second_cycle_error=RuntimeError("second cycle"),
        )
        assert calls["n"] == 2
        assert sleeper.await_count == 1, (
            "with nobody to report to, back off rather than hot-looping on a "
            "cert the API has already refused"
        )

    @pytest.mark.asyncio
    async def test_a_reported_rejection_that_did_not_re_join_still_backs_off(self):
        """Below the threshold there is no new identity, so do not hot-loop."""
        calls, sleeper = await _drive_renew_loop(
            on_cert_rejected=AsyncMock(return_value=False),
            second_cycle_error=RuntimeError("second cycle"),
        )
        assert calls["n"] == 2
        assert sleeper.await_count == 1


class TestA403OnRenewIsTransientNotARejection:
    """#2352. A 403 used to raise `_CertRejected`, and that was destructive.

    Traced against the route rather than assumed: `get_listener_identity`
    answers 401 for every authentication failure and never 403, and the renew
    handler's only 403 is "Certificate does not match the listener id in the
    path" -- which a listener cannot provoke, because it builds that path from
    its own `identity.listener_id`. So every 403 a listener can actually see on
    this endpoint comes from outside the API: an ingress, a WAF, a mesh, an
    auth sidecar.

    Treating one as a certificate rejection made the listener delete its
    credentials Secret and spend a join-token use, against a certificate that
    was still perfectly valid. On a pool whose token has `max_uses: 1` that is
    unrecoverable -- an infrastructure blip permanently removing a listener.
    """

    @pytest.mark.asyncio
    async def test_a_403_is_retried_and_the_cert_survives(self):
        ident = _identity()
        new_data = {"certificate": "PEM", "private_key": "K", "ca_certificate": "CA"}

        forbidden = MagicMock()
        forbidden.status_code = 403
        forbidden.text = "blocked by proxy"
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"data": new_data}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = [forbidden, ok]

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await identity_mod._call_renew_with_retries(ident)

        assert result == new_data, "a proxy 403 must not end the renewal"
        assert mock_client.post.call_count == 2, "the 403 was not retried"

    @pytest.mark.asyncio
    async def test_a_403_never_raises_cert_rejected(self):
        """The property, stated separately from the retry: `_CertRejected` is
        what drives Secret deletion and a join-token use, so a 403 must not
        produce one even when every attempt returns 403."""
        ident = _identity()

        forbidden = MagicMock()
        forbidden.status_code = 403
        forbidden.text = "blocked by proxy"

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = forbidden

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await identity_mod._call_renew_with_retries(ident)

        assert result is None, "exhausted retries report transient failure, not rejection"
        assert mock_client.post.call_count > 1, "403 was treated as terminal"

    @pytest.mark.asyncio
    async def test_a_401_still_raises_so_a_real_rejection_still_recovers(self):
        """The control. Narrowing to 401 must not have disarmed the mechanism
        that recovers a genuinely expired certificate."""
        ident = _identity()

        unauthorized = MagicMock()
        unauthorized.status_code = 401
        unauthorized.text = "expired"

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = unauthorized

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(identity_mod._CertRejected):
                await identity_mod._call_renew_with_retries(ident)

        assert mock_client.post.call_count == 1


class TestRejoinIdentity:
    """A re-join must not destroy the credential it is trying to replace.

    `_rejoin_unlocked` used to call `clear_credentials_secret()` and then
    `establish_identity()`. The delete always succeeded; the join could fail for
    a reason the listener cannot influence, and `_call_join` collapses *revoked*,
    *exhausted* and **expired** into one `_JoinTokenExhausted` -- so with
    `default_join_token_ttl_seconds: 3600` every listener alive longer than an
    hour held a token that could not join, and deleted a cert it still held in
    order to discover that. A pod restart then found no Secret and no usable
    token and crash-looped.

    Nothing covered it, because every test in `test_listener_rejoin.py` patches
    `_rejoin_unlocked` itself -- those test the gate, and this tests the
    mechanism the gate calls.
    """

    @staticmethod
    def _join_response(lid: str, pid: str) -> dict:
        return {
            "listener_id": lid,
            "pool_id": pid,
            "certificate": "NEWPEM",
            "private_key": "NEWK",
            "ca_certificate": "NEWCA",
        }

    @pytest.mark.asyncio
    async def test_a_failed_join_leaves_the_credentials_secret_untouched(self):
        """The defect: a join that fails must cost the listener nothing."""
        held = _identity()
        held.secret_resource_version = "100"
        ours = _v1secret(listener_id=held.listener_id, pool_id=held.pool_id, rv="100")

        env = {"TERRAPOD_JOIN_TOKEN": "a-dead-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=ours),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(side_effect=identity_mod._JoinTokenExhausted("expired")),
            ),
            patch.object(identity_mod, "_replace_secret") as replace,
            patch.object(identity_mod, "_create_secret") as create,
        ):
            with pytest.raises(identity_mod._JoinTokenExhausted):
                await identity_mod.rejoin_identity(_RC, current=held)

        # Nothing was written, and -- the whole point -- nothing was deleted.
        replace.assert_not_called()
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_successful_join_replaces_the_secret_under_cas(self):
        held = _identity()
        held.secret_resource_version = "100"
        ours = _v1secret(listener_id=held.listener_id, pool_id=held.pool_id, rv="100")
        lid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        written = _v1secret(listener_id=lid, pool_id=pid, rv="101")

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=ours),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(return_value=self._join_response(lid, pid)),
            ),
            patch.object(identity_mod, "_replace_secret", return_value=written) as replace,
        ):
            result = await identity_mod.rejoin_identity(_RC, current=held)

        assert str(result.listener_id) == lid
        assert result.certificate_pem == "NEWPEM"
        assert result.secret_resource_version == "101"
        # CAS on the version we read, so a sibling writing meanwhile loses us the
        # race rather than silently overwriting its identity.
        assert replace.call_args.kwargs["resource_version"] == "100"

    @pytest.mark.asyncio
    async def test_a_secret_that_moved_since_ours_is_adopted_without_joining(self):
        """A sibling already re-joined, so we must not spend a second token use."""
        held = _identity()
        held.secret_resource_version = "100"
        sib_lid, sib_pid = uuid.uuid4(), uuid.uuid4()
        theirs = _v1secret(listener_id=sib_lid, pool_id=sib_pid, cert="SIBPEM", rv="200")

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=theirs),
            patch.object(identity_mod, "_call_join", new=AsyncMock()) as join,
            patch.object(identity_mod, "_replace_secret") as replace,
        ):
            result = await identity_mod.rejoin_identity(_RC, current=held)

        assert result.listener_id == sib_lid
        assert result.certificate_pem == "SIBPEM"
        join.assert_not_called()
        replace.assert_not_called()

    @pytest.mark.asyncio
    async def test_our_own_rejected_secret_is_not_adopted(self):
        """The contrast that makes the test above mean anything.

        A bare "a Secret exists, adopt it" would adopt the very cert being
        rejected and never join at all -- an infinite rejection loop. Only a
        resourceVersion that has MOVED past the one we hold is a sibling's work.
        """
        held = _identity()
        held.secret_resource_version = "100"
        ours = _v1secret(listener_id=held.listener_id, pool_id=held.pool_id, rv="100")
        lid, pid = str(uuid.uuid4()), str(uuid.uuid4())

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=ours),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(return_value=self._join_response(lid, pid)),
            ) as join,
            patch.object(
                identity_mod,
                "_replace_secret",
                return_value=_v1secret(listener_id=lid, pool_id=pid, rv="101"),
            ),
        ):
            result = await identity_mod.rejoin_identity(_RC, current=held)

        join.assert_called_once()
        assert str(result.listener_id) == lid

    @pytest.mark.asyncio
    async def test_an_absent_secret_is_created_rather_than_replaced(self):
        lid, pid = str(uuid.uuid4()), str(uuid.uuid4())
        created = _v1secret(listener_id=lid, pool_id=pid, rv="1")

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=None),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(return_value=self._join_response(lid, pid)),
            ),
            patch.object(identity_mod, "_create_secret", return_value=created) as create,
            patch.object(identity_mod, "_replace_secret") as replace,
        ):
            result = await identity_mod.rejoin_identity(_RC, current=None)

        create.assert_called_once()
        replace.assert_not_called()
        assert str(result.listener_id) == lid

    @pytest.mark.asyncio
    async def test_a_lost_cas_race_adopts_the_winner(self):
        held = _identity()
        held.secret_resource_version = "100"
        ours = _v1secret(listener_id=held.listener_id, pool_id=held.pool_id, rv="100")
        win_lid, win_pid = uuid.uuid4(), uuid.uuid4()
        winner = _v1secret(listener_id=win_lid, pool_id=win_pid, cert="WINPEM", rv="200")

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", side_effect=[ours, winner]),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(
                    return_value=self._join_response(str(uuid.uuid4()), str(uuid.uuid4()))
                ),
            ),
            patch.object(
                identity_mod,
                "_replace_secret",
                side_effect=identity_mod._SecretConflict("stale"),
            ),
        ):
            result = await identity_mod.rejoin_identity(_RC, current=held)

        assert result.listener_id == win_lid
        assert result.certificate_pem == "WINPEM"

    @pytest.mark.asyncio
    async def test_a_missing_join_token_raises_before_touching_the_secret(self):
        held = _identity()
        held.secret_resource_version = "100"
        ours = _v1secret(listener_id=held.listener_id, pool_id=held.pool_id, rv="100")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", return_value=ours),
            patch.object(identity_mod, "_call_join", new=AsyncMock()) as join,
            patch.object(identity_mod, "_replace_secret") as replace,
        ):
            with pytest.raises(RuntimeError, match="TERRAPOD_JOIN_TOKEN"):
                await identity_mod.rejoin_identity(_RC, current=held)

        join.assert_not_called()
        replace.assert_not_called()

    @pytest.mark.asyncio
    async def test_losing_the_create_race_also_adopts_the_winner(self):
        """The `_SecretAlreadyExists` half of the same handler.

        Reachable when an operator has run the manual identity reset
        (`kubectl delete secret ...`) and two pods re-join at once: both read no
        Secret, both join, and the loser's `_create_secret` gets a 409. It shares
        one `except` with the replace path's `_SecretConflict`, so dropping just
        this name from the tuple would leave the loser raising instead of
        adopting -- and no test of the conflict path can see that, which is the
        gate-written-for-one-instance-of-a-class shape.
        """
        win_lid, win_pid = uuid.uuid4(), uuid.uuid4()
        winner = _v1secret(listener_id=win_lid, pool_id=win_pid, cert="WINPEM", rv="7")

        env = {"TERRAPOD_JOIN_TOKEN": "a-live-token"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(identity_mod, "_read_in_pod_namespace", return_value="terrapod"),
            patch.object(identity_mod, "_read_secret", side_effect=[None, winner]),
            patch.object(
                identity_mod,
                "_call_join",
                new=AsyncMock(
                    return_value=self._join_response(str(uuid.uuid4()), str(uuid.uuid4()))
                ),
            ),
            patch.object(
                identity_mod,
                "_create_secret",
                side_effect=identity_mod._SecretAlreadyExists("exists"),
            ),
        ):
            result = await identity_mod.rejoin_identity(_RC, current=None)

        assert result.listener_id == win_lid
        assert result.certificate_pem == "WINPEM"
