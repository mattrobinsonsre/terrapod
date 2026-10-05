"""The cloud-identity mint endpoint (#1901).

The security-critical property is that **the phase comes from the presented
runner token and never from the request**: a plan-phase Job asking for the apply
identity is the thing the apply increment exists to prevent, and it is only
prevented if the server refuses to take the caller's word for which phase it is.

The second property is that the three outcomes stay distinguishable. The runner
behaves completely differently on each — take no action, deliver the token, fail
the run — so collapsing "this workspace mints nothing" into an error, or into a
200 with an empty token, breaks the feature in opposite directions.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import cloud_identity as router


def _user(*, method: str = "runner_token", run_id: str | None = None, phase: str | None = None):
    return AuthenticatedUser(
        email="runner" if method == "runner_token" else "user@terrapod",
        display_name=None,
        roles=["everyone"],
        provider_name="runner_token" if method == "runner_token" else "local",
        auth_method=method,
        run_id=run_id,
        run_phase=phase,
    )


def _ws(name="dns-prod"):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.name = name
    return m


def _run(ws, *, audiences):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.workspace_id = ws.id
    m.oidc_audiences = audiences
    return m


def _db(run, ws):
    db = MagicMock()

    async def _get(model, pk):
        from terrapod.db.models import Run, Workspace

        if model is Run:
            return run
        if model is Workspace:
            return ws
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _enabled(**over):
    cfg = MagicMock()
    cfg.enabled = over.get("enabled", True)
    cfg.token_ttl_seconds = over.get("ttl", 900)
    return cfg


async def _call(user, run, ws, *, issuer="https://terrapod.example.com", cfg=None):
    settings = MagicMock()
    settings.auth.oidc_issuer = cfg or _enabled()
    captured: dict = {}

    def _sign(claims, *, ttl_seconds):
        captured["claims"] = claims
        captured["ttl"] = ttl_seconds
        return "signed.jwt.value"

    with (
        patch("terrapod.config.settings", settings),
        patch("terrapod.auth.oidc_signing.sign_identity_token", _sign),
        patch("terrapod.api.routers.oidc_issuer.issuer_url", return_value=issuer),
    ):
        resp = await router.mint_cloud_identity_token(
            run_id=f"run-{run.id}", user=user, db=_db(run, ws)
        )
    return resp, captured


class TestTheWorkspaceMintsNothing:
    """204, not an error and not an empty 200."""

    async def test_an_empty_audience_list_is_204(self):
        ws = _ws()
        run = _run(ws, audiences=[])
        resp, captured = await _call(_user(run_id=str(run.id), phase="plan"), run, ws)
        assert resp.status_code == 204
        assert "claims" not in captured, "nothing should have been signed"

    async def test_a_deployment_with_no_issuer_is_204(self):
        """An operator who has not published an issuer has not opted in at all,
        so this is not a failure the runner should fail the run on."""
        ws = _ws()
        run = _run(ws, audiences=["sts.amazonaws.com"])
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, cfg=_enabled(enabled=False)
        )
        assert resp.status_code == 204


class TestThePhaseComesFromTheToken:
    """The security property. A request body cannot influence it, because there
    is no request body — and the claim is built from `user.run_phase`, which is
    whatever the presented token was signed with."""

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    async def test_the_claim_mirrors_the_token(self, phase):
        ws = _ws()
        run = _run(ws, audiences=["sts.amazonaws.com"])
        resp, captured = await _call(_user(run_id=str(run.id), phase=phase), run, ws)
        assert resp.status_code == 200
        assert captured["claims"]["phase"] == phase
        assert captured["claims"]["sub"] == f"workspace:dns-prod:phase:{phase}"

    async def test_the_endpoint_takes_no_body_at_all(self):
        """Asserted on the signature rather than behaviourally: a `body`
        parameter is the thing that would let a runner name its own phase, so
        its ABSENCE is the guarantee. A behavioural test cannot see a parameter
        that was added but ignored today and read tomorrow."""
        import inspect

        params = inspect.signature(router.mint_cloud_identity_token).parameters
        assert "body" not in params
        assert set(params) == {"run_id", "user", "db"}

    async def test_a_token_with_no_phase_claim_mints_a_token_with_no_phase(self):
        """A runner token minted before the phase claim existed carries none.
        That must read as "makes no claim", so the JWT carries no phase either
        and an operator's trust condition on it simply will not match — refusing
        the credential rather than quietly widening it to the apply identity."""
        ws = _ws()
        run = _run(ws, audiences=["sts.amazonaws.com"])
        resp, captured = await _call(_user(run_id=str(run.id), phase=None), run, ws)
        assert resp.status_code == 200
        assert "phase" not in captured["claims"]
        # And `sub` falls back to the workspace alone rather than inventing one.
        assert captured["claims"]["sub"] == "workspace:dns-prod"


class TestTheClaimSet:
    async def test_it_carries_what_a_trust_policy_conditions_on(self):
        ws = _ws()
        run = _run(ws, audiences=["sts.amazonaws.com", "api://AzureADTokenExchange"])
        resp, captured = await _call(_user(run_id=str(run.id), phase="apply"), run, ws)
        claims = captured["claims"]

        assert claims["iss"] == "https://terrapod.example.com"
        assert claims["aud"] == ["sts.amazonaws.com", "api://AzureADTokenExchange"]
        assert claims["workspace"] == "dns-prod"
        assert claims["workspace_id"] == str(ws.id)
        assert claims["run_id"] == str(run.id)
        assert claims["terrapod_organization"] == "default"
        assert captured["ttl"] == 900

    async def test_the_audiences_come_from_the_RUN_not_the_workspace(self):
        """The run's snapshot is authoritative. An operator editing the
        workspace mid-run would otherwise let the plan phase mint a token and
        the apply phase be refused."""
        ws = _ws()
        ws.oidc_audiences = ["edited-after-the-run-started"]
        run = _run(ws, audiences=["as-at-run-creation"])
        _, captured = await _call(_user(run_id=str(run.id), phase="plan"), run, ws)
        assert captured["claims"]["aud"] == ["as-at-run-creation"]

    async def test_no_credential_material_is_in_the_claims(self):
        """Claims are published to a third party by definition — the cloud reads
        them. Nothing resembling a secret belongs there."""
        ws = _ws()
        run = _run(ws, audiences=["a"])
        _, captured = await _call(_user(run_id=str(run.id), phase="plan"), run, ws)
        for forbidden in ("token", "secret", "key", "password", "credential"):
            assert not any(forbidden in k.lower() for k in captured["claims"]), forbidden


class TestTheAuthBoundary:
    async def test_a_session_user_is_refused(self):
        """Runner protocol only. A person has no business minting a run's cloud
        identity, and `require_runner_for_run` is what says so."""
        from fastapi import HTTPException

        ws = _ws()
        run = _run(ws, audiences=["a"])
        with pytest.raises(HTTPException) as exc:
            await _call(_user(method="session"), run, ws)
        assert exc.value.status_code == 403

    async def test_a_runner_token_for_a_different_run_is_refused(self):
        """A leaked token from run A must not mint an identity for run B — which
        would be an identity for a different WORKSPACE, so this is the boundary
        the whole feature rests on."""
        from fastapi import HTTPException

        ws = _ws()
        run = _run(ws, audiences=["a"])
        other = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=other, phase="plan"), run, ws)
        assert exc.value.status_code == 403
