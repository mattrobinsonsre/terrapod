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

# establish_identity()/clear_credentials_secret() now take their non-sensitive
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
        private_key_pem="key",
        ca_cert_pem="ca",
    )


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
