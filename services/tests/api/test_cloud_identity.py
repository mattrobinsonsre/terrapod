"""The cloud-identity mint endpoint (#1901).

The security-critical property is that **the phase comes from the presented
runner token and never from the request**: a plan-phase Job asking for the apply
identity is the thing the apply increment exists to prevent, and it is only
prevented if the server refuses to take the caller's word for which phase it is.

The second property is that **one token carries one target's audiences**. A
token audienced for several targets is replayable between them, and AWS refuses
a multi-valued `aud` outright, so a test that lets a second target's audience
into `aud` is testing the bug this shape exists to remove.

The third is that the outcomes stay distinguishable. The runner behaves
completely differently on each — take no action, deliver the token, fail the run
— so collapsing "nothing maps here" into an error, or an error into a 204,
breaks the feature in opposite directions. **A target-less request is 204 and
not 400**, because that is a lagging runner image and the designed behaviour is
that it falls through to the agent pool's identity rather than failing the run.

Fixtures are production-shaped on purpose: the run's snapshot is DERIVED from
the catalogue and the workspace override by the same resolver the API uses, so
the unchanged case is unchanged by construction and a test that wants the
configuration to have moved has to say so explicitly. A fixture that set the
snapshot independently would make every happy-path test a coin toss on whether
it had accidentally written a 409 scenario.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import cloud_identity as router
from terrapod.services import cloud_identity_resolver

AWS = "sts.amazonaws.com"
AZURE = "api://AzureADTokenExchange"


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


def _enabled(**over):
    cfg = MagicMock()
    cfg.enabled = over.get("enabled", True)
    cfg.token_ttl_seconds = over.get("ttl", 900)
    # A real dict, not a MagicMock attribute: the resolver reads it, and a
    # MagicMock would resolve to {} and turn every test into a 409.
    cfg.audiences = over.get("audiences", {})
    return cfg


def _scenario(*, catalogue=None, override=None, snapshot=None, name="dns-prod"):
    """A workspace, its run and the deployment config, wired as production does.

    `snapshot` defaults to what the resolver produces from `catalogue` and
    `override` — which is exactly what run creation stores — so pass it only to
    express "the configuration has moved since this run was created".
    """
    catalogue = catalogue if catalogue is not None else {}
    override = override if override is not None else {}

    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = name
    ws.oidc_audiences = override

    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = ws.id
    run.oidc_audiences = (
        snapshot if snapshot is not None else cloud_identity_resolver.merge(catalogue, override)
    )

    return ws, run, _enabled(audiences=catalogue)


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
    # The mint WRITES now — it records the target it served, which is what the
    # confirm-time staleness check is scoped to. A MagicMock here is not
    # awaitable, so this fixture has to match production rather than the route's
    # earlier read-only shape.
    db.commit = AsyncMock()
    return db


async def _call(user, run, ws, *, target="aws", issuer="https://terrapod.example.com", cfg=None):
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
            run_id=f"run-{run.id}", target=target, user=user, db=_db(run, ws)
        )
    return resp, captured


class TestNothingToDeliver:
    """204, not an error and not an empty 200. Four distinct reasons."""

    async def test_an_empty_mapping_is_204(self):
        ws, run, cfg = _scenario()
        resp, captured = await _call(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        assert resp.status_code == 204
        assert "claims" not in captured, "nothing should have been signed"

    async def test_a_deployment_with_no_issuer_is_204(self):
        """An operator who has not published an issuer has not opted in at all,
        so this is not a failure the runner should fail the run on."""
        ws, run, _ = _scenario(catalogue={"aws": [AWS]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, cfg=_enabled(enabled=False)
        )
        assert resp.status_code == 204

    async def test_a_target_nothing_maps_to_is_204(self):
        """The common answer: most providers in most workspaces federate to
        nothing, and the runner asks about every one it discovered."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="google", cfg=cfg
        )
        assert resp.status_code == 204
        assert "claims" not in captured

    async def test_a_request_with_NO_target_is_204_not_400(self):
        """A runner image older than per-target minting sends no target.

        400 is the obvious answer and it would make every run on a lagging
        runner FAIL, when the designed behaviour is falling through to the agent
        pool's own identity exactly as before this feature existed. The
        fall-through is permanent and supported, so a request we cannot serve
        has to look like "nothing here" rather than like a fault.
        """
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="", cfg=cfg
        )
        assert resp.status_code == 204
        assert "claims" not in captured


class TestOneTargetPerToken:
    async def test_aud_carries_ONLY_the_requested_target(self):
        """The whole point of the shape. A second target's audience in `aud`
        makes the token replayable against it — and AWS refuses a multi-valued
        `aud` outright, so this is both a security and a function property."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azurerm": [AZURE]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg
        )
        assert resp.status_code == 200
        assert captured["claims"]["aud"] == [AWS]
        assert AZURE not in captured["claims"]["aud"]

    async def test_a_grouped_entry_keeps_its_several_audiences(self):
        """A list within ONE entry is the deliberate "these are
        interchangeable" statement — the Vault case, where `bound_audiences`
        intersects. It is not the multi-target replay above."""
        ws, run, cfg = _scenario(catalogue={"vault": ["tp-a", "tp-b"]})
        _r, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="vault", cfg=cfg
        )
        assert captured["claims"]["aud"] == ["tp-a", "tp-b"]

    async def test_an_alias_resolves_specific_before_general(self):
        ws, run, cfg = _scenario(catalogue={"vault": ["general"], "vault.eu": ["eu-only"]})
        _r, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="vault.eu", cfg=cfg
        )
        assert captured["claims"]["aud"] == ["eu-only"]

    async def test_an_alias_with_no_entry_of_its_own_falls_back_to_the_provider(self):
        """What lets an operator alias a provider five times without naming
        every alias in the catalogue."""
        ws, run, cfg = _scenario(catalogue={"vault": ["general"]})
        _r, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="vault.us", cfg=cfg
        )
        assert captured["claims"]["aud"] == ["general"]

    async def test_a_workspace_override_replaces_the_catalogue_entry(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": ["narrowed"]})
        _r, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["aud"] == ["narrowed"], "the override replaces, not appends"


class TestTheConfigurationMovedUnderTheRun:
    """The controlled failure. A 409 before anything executes, rather than a
    token the cloud has stopped accepting failing inside the engine."""

    async def test_a_changed_target_is_refused(self):
        from fastapi import HTTPException

        # Snapshot says one thing; the live catalogue now says another.
        ws, run, cfg = _scenario(
            catalogue={"aws": ["rotated-to-this"]}, snapshot={"aws": ["as-at-run-creation"]}
        )
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 409
        # The operator's action, not a description of a mismatch.
        assert "new run" in exc.value.detail.lower()

    async def test_a_target_removed_since_run_creation_is_refused(self):
        """Not a 204. The run was created expecting an identity for this target,
        so answering "nothing here" would silently drop it to the pool's
        identity — broader than the one the operator chose."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={}, snapshot={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 409

    async def test_a_workspace_override_edited_mid_run_is_refused(self):
        """Previously this asserted the opposite — that the run's snapshot won
        and minting proceeded. That was the bug: it hands the apply a token
        matching the reviewed plan while the cloud has moved on."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(
            catalogue={"aws": [AWS]},
            override={"aws": ["edited-after-the-run-started"]},
            snapshot={"aws": [AWS]},
        )
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 409

    async def test_an_UNRELATED_change_does_not_refuse(self):
        """The check is narrow on purpose. A catalogue edit touching a provider
        this run never uses must not refuse an apply that nothing invalidated —
        otherwise every unrelated change blocks every queued apply."""
        ws, run, cfg = _scenario(
            catalogue={"aws": [AWS], "google": ["added-later"]},
            snapshot={"aws": [AWS]},
        )
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert resp.status_code == 200
        assert captured["claims"]["aud"] == [AWS]


class TestThePhaseComesFromTheToken:
    """The security property. A request body cannot influence it, because there
    is no request body — and the claim is built from `user.run_phase`, which is
    whatever the presented token was signed with."""

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    async def test_the_claim_mirrors_the_token(self, phase):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase=phase), run, ws, target="aws", cfg=cfg
        )
        assert resp.status_code == 200
        assert captured["claims"]["phase"] == phase
        assert captured["claims"]["sub"] == f"workspace:dns-prod:phase:{phase}"

    async def test_the_endpoint_takes_no_body_at_all(self):
        """Asserted on the signature rather than behaviourally: a `body`
        parameter is the thing that would let a runner name its own phase, so
        its ABSENCE is the guarantee. A behavioural test cannot see a parameter
        that was added but ignored today and read tomorrow.

        `target` is a query parameter and names a provider configuration, not an
        identity — it selects which of the run's own already-resolved entries to
        mint, and cannot introduce one.
        """
        import inspect

        params = inspect.signature(router.mint_cloud_identity_token).parameters
        assert "body" not in params
        assert set(params) == {"run_id", "target", "user", "db"}

    async def test_a_token_with_no_phase_claim_mints_a_token_with_no_phase(self):
        """A runner token minted before the phase claim existed carries none.
        That must read as "makes no claim", so the JWT carries no phase either
        and an operator's trust condition on it simply will not match — refusing
        the credential rather than quietly widening it to the apply identity."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase=None), run, ws, target="aws", cfg=cfg
        )
        assert resp.status_code == 200
        assert "phase" not in captured["claims"]
        # And `sub` falls back to the workspace alone rather than inventing one.
        assert captured["claims"]["sub"] == "workspace:dns-prod"


class TestTheClaimSet:
    async def test_it_carries_what_a_trust_policy_conditions_on(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        claims = captured["claims"]

        assert claims["iss"] == "https://terrapod.example.com"
        assert claims["aud"] == [AWS]
        assert claims["workspace"] == "dns-prod"
        assert claims["workspace_id"] == str(ws.id)
        assert claims["run_id"] == str(run.id)
        assert claims["terrapod_organization"] == "default"
        assert captured["ttl"] == 900

    async def test_the_response_echoes_the_target(self):
        """So the runner writes the file under the name it asked for rather than
        re-deriving it, and a log line can name the target without the token."""
        ws, run, cfg = _scenario(catalogue={"vault": ["tp"]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="vault.eu", cfg=cfg
        )
        import json

        assert json.loads(resp.body)["target"] == "vault.eu"

    async def test_no_credential_material_is_in_the_claims(self):
        """Claims are published to a third party by definition — the cloud reads
        them. Nothing resembling a secret belongs there."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        _r, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg
        )
        for forbidden in ("token", "secret", "key", "password", "credential"):
            assert not any(forbidden in k.lower() for k in captured["claims"]), forbidden


class TestTheAuthBoundary:
    async def test_a_session_user_is_refused(self):
        """Runner protocol only. A person has no business minting a run's cloud
        identity, and `require_runner_for_run` is what says so."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call(_user(method="session"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 403

    async def test_a_runner_token_for_a_different_run_is_refused(self):
        """A leaked token from run A must not mint an identity for run B — which
        would be an identity for a different WORKSPACE, so this is the boundary
        the whole feature rests on."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        other = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=other, phase="plan"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 403


async def _call_targets(user, run, ws, *, cfg=None):
    settings = MagicMock()
    settings.auth.oidc_issuer = cfg or _enabled()
    with patch("terrapod.config.settings", settings):
        return await router.list_cloud_identity_targets(
            run_id=f"run-{run.id}", user=user, db=_db(run, ws)
        )


class TestTheTargetsRoute:
    """Names only, from the snapshot, so the runner can skip the engine.

    This route exists so a workspace that mints nothing never invokes `tofu
    graph` — which is what keeps the feature from adding cost, or a new way to
    fail, to the overwhelming majority of runs. It is also why failing closed on
    a discovery error is correct: by the time the runner asks the engine, this
    route has already said the operator asked for federation.
    """

    async def test_the_configured_targets_are_listed_sorted(self):
        ws, run, cfg = _scenario(catalogue={"vault": ["https://vault"], "aws": [AWS]})
        resp = await _call_targets(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        assert resp.status_code == 200
        import json

        assert json.loads(resp.body)["targets"] == ["aws", "vault"]

    async def test_the_audiences_are_never_returned(self):
        """An audience is the value a cloud trust policy matches on, so the set
        of them names the roles this deployment can ask to assume. The runner
        writes a file and the engine reads it — it has no use for them, so they
        stay in the mint response and are never enumerable."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azure": [AZURE]})
        resp = await _call_targets(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        body = resp.body.decode()
        assert AWS not in body
        assert AZURE not in body

    async def test_an_empty_mapping_is_204(self):
        ws, run, cfg = _scenario()
        resp = await _call_targets(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        assert resp.status_code == 204

    async def test_a_disabled_issuer_is_204(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.enabled = False
        resp = await _call_targets(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        assert resp.status_code == 204

    async def test_the_snapshot_is_listed_not_live_configuration(self):
        """A target added to the workspace after this run was created is
        deliberately absent: the plan was reviewed without it, and the mint
        would refuse it anyway. Listing live configuration would have the runner
        discover a target it then could not mint."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, snapshot={"aws": [AWS]})
        ws.oidc_audiences = {"azure": [AZURE]}  # added since the run was created
        resp = await _call_targets(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg)
        import json

        assert json.loads(resp.body)["targets"] == ["aws"]

    async def test_a_session_user_is_refused(self):
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call_targets(_user(method="session"), run, ws, cfg=cfg)
        assert exc.value.status_code == 403

    async def test_a_runner_token_for_a_different_run_is_refused(self):
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call_targets(_user(run_id=str(uuid.uuid4()), phase="plan"), run, ws, cfg=cfg)
        assert exc.value.status_code == 403


async def _call_defaults(user, *, cfg=None):
    settings = MagicMock()
    settings.auth.oidc_issuer = cfg or _enabled()
    with patch("terrapod.config.settings", settings):
        return await router.get_oidc_audience_defaults(user=user)


class TestTheAudienceDefaults:
    """What a workspace's own map merges OVER.

    Exists so the two-level merge is observable: a workspace read returns the
    merged map with no marker for which entries the workspace owns, so without
    this an operator cannot tell an inherited entry from one of their own.
    """

    async def test_the_catalogue_is_returned(self):
        import json

        cfg = _enabled(audiences={"aws": [AWS], "vault": ["https://vault.example.com"]})
        resp = await _call_defaults(_user(method="session"), cfg=cfg)
        body = json.loads(resp.body)["data"]["attributes"]
        assert body["audiences"] == {"aws": [AWS], "vault": ["https://vault.example.com"]}

    async def test_an_unconfigured_catalogue_is_empty_not_an_error(self):
        import json

        resp = await _call_defaults(_user(method="session"), cfg=_enabled(audiences={}))
        assert resp.status_code == 200
        assert json.loads(resp.body)["data"]["attributes"]["audiences"] == {}

    async def test_the_issuer_state_is_reported_separately_from_the_catalogue(self):
        """An empty catalogue and a disabled issuer are different things, and an
        operator debugging "why did my workspace mint nothing" needs to tell
        them apart."""
        import json

        cfg = _enabled(audiences={"aws": [AWS]})
        cfg.enabled = False
        attrs = json.loads((await _call_defaults(_user(method="session"), cfg=cfg)).body)["data"][
            "attributes"
        ]
        assert attrs["issuer-enabled"] is False
        assert attrs["audiences"] == {"aws": [AWS]}

    async def test_the_response_hands_out_copies_not_the_live_config_lists(self):
        """The lists are copied. A serializer handing out the live config
        object's own lists lets anything downstream of it edit process-wide
        settings — so this captures what the route actually built and asserts
        it is not the same object, rather than asserting on a dict the test
        made itself."""
        catalogue = {"aws": [AWS]}
        cfg = _enabled(audiences=catalogue)

        captured: dict = {}

        class _Capture:
            def __init__(self, content=None, **kw):
                captured["content"] = content
                self.status_code = kw.get("status_code", 200)

        settings = MagicMock()
        settings.auth.oidc_issuer = cfg
        with (
            patch("terrapod.config.settings", settings),
            patch.object(router, "JSONResponse", _Capture),
        ):
            await router.get_oidc_audience_defaults(user=_user(method="session"))

        served = captured["content"]["data"]["attributes"]["audiences"]
        assert served == {"aws": [AWS]}, "the catalogue was not served correctly"
        assert served["aws"] is not catalogue["aws"], (
            "the route handed out the live config object's own list"
        )
        served["aws"].append("injected")
        assert catalogue["aws"] == [AWS], "mutating the served value reached live settings"

    async def test_a_runner_token_is_not_the_intended_caller_but_is_authenticated(self):
        """Documented asymmetry: the runner-facing targets route returns names
        only, because a runner writes a file and the engine reads it. This route
        is for a person composing configuration. It does not special-case the
        runner, and does not need to — the runner never calls it."""
        resp = await _call_defaults(_user(run_id="r", phase="plan"), cfg=_enabled(audiences={}))
        assert resp.status_code == 200


class TestTheMintRecordsWhatItServed:
    """`Run.oidc_minted_targets` is what the confirm-time staleness check is
    scoped to, so it has to be written by the only thing that knows: the mint.

    Recorded rather than derived from the configured snapshot, because that
    snapshot is the MERGED map and carries deployment-wide catalogue entries a
    workspace may never use. Checking against it would let one edit to the
    catalogue refuse every pending apply in the fleet.
    """

    async def test_a_served_target_is_recorded(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        run.oidc_minted_targets = []
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg
        )
        assert resp.status_code == 200
        assert run.oidc_minted_targets == ["aws"]

    async def test_several_targets_accumulate(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azure": [AZURE]})
        run.oidc_minted_targets = []
        for t in ("aws", "azure"):
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, target=t, cfg=cfg)
        assert run.oidc_minted_targets == ["aws", "azure"]

    async def test_a_repeat_mint_does_not_duplicate(self):
        """An apply phase re-mints the same targets the plan did, and a retry
        re-mints too. The set only grows, so a duplicate would be harmless —
        but it would also make the recorded list unbounded over a long run."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        run.oidc_minted_targets = []
        for _ in range(3):
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg)
        assert run.oidc_minted_targets == ["aws"]

    async def test_a_target_that_maps_to_nothing_is_not_recorded(self):
        """204, so nothing was served. Recording it would make the confirm check
        refuse an apply over an identity the plan never presented."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        run.oidc_minted_targets = []
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="gcp", cfg=cfg
        )
        assert resp.status_code == 204
        assert run.oidc_minted_targets == []

    async def test_a_refused_mint_is_not_recorded(self):
        """A 409 means the configuration moved, so no token was issued."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, snapshot={"aws": ["sts.old.example"]})
        run.oidc_minted_targets = []
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 409
        assert run.oidc_minted_targets == []


class TestTheWorkspaceReadIsMerged:
    """What a client gets back is the workspace's override resolved OVER the
    deployment catalogue, per key — not the stored override.

    The override alone is unreadable on its own: a key's absence means
    "inherit", which a reader cannot distinguish from "nothing here". So the
    read has to be the effective map, and the cost of that lands on the
    provider, which reconciles only the keys it owns.
    """

    def _serialized(self, *, override, catalogue):
        from terrapod.api.routers import tfe_v2

        ws = MagicMock()
        ws.oidc_audiences = override
        settings = MagicMock()
        settings.auth.oidc_issuer = _enabled(audiences=catalogue)
        with patch("terrapod.config.settings", settings):
            return tfe_v2._merged_oidc_audiences(ws)

    def test_an_inherited_key_appears_in_the_read(self):
        got = self._serialized(override={"aws": [AWS]}, catalogue={"vault": ["https://v"]})
        assert got == {"aws": [AWS], "vault": ["https://v"]}

    def test_the_workspace_override_wins_per_key(self):
        got = self._serialized(
            override={"aws": ["sts.override"]}, catalogue={"aws": [AWS], "vault": ["https://v"]}
        )
        assert got["aws"] == ["sts.override"]
        assert got["vault"] == ["https://v"], "an unrelated catalogue key was lost"

    def test_removing_a_key_falls_back_to_the_catalogue(self):
        """The decided semantics: an absent override key inherits rather than
        meaning 'none here'."""
        got = self._serialized(override={}, catalogue={"aws": [AWS]})
        assert got == {"aws": [AWS]}

    def test_no_catalogue_returns_the_override_alone(self):
        got = self._serialized(override={"aws": [AWS]}, catalogue={})
        assert got == {"aws": [AWS]}

    def test_neither_configured_is_empty_not_an_error(self):
        assert self._serialized(override={}, catalogue={}) == {}

    def test_the_read_cannot_mutate_the_stored_override(self):
        """The serializer runs on every workspace of every list response, so a
        shared list handed out here would be editable through any one of them."""
        override = {"aws": [AWS]}
        got = self._serialized(override=override, catalogue={})
        got["aws"].append("injected")
        assert override["aws"] == [AWS], "mutating the served value reached the ORM object"
