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
completely differently on each — take no action, deliver the tokens, fail the
run — so collapsing "nothing maps here" into an error, or an error into a 204,
breaks the feature in opposite directions. **A request naming no providers is
204 and not 400**, because a configuration may legitimately declare none, and
failing it would break a run that was never using this feature.

The fourth arrived with the restructure: the runner discovers unconditionally
and reports whether it could trust its own answer, because only this end knows
whether a graph it could not read matters. **The order is load-bearing** — a
workspace holding no identity is answered 204 before the outcome is examined,
so a graph failure never fails a run that does not federate.

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


def _scenario(*, catalogue=None, override=None, snapshot=None, name="dns-prod", engine="terraform"):
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
    # Explicit, because the route reads it to decide whether the runner could
    # have discovered anything (#2006). Left as a MagicMock attribute it would
    # resolve to an unknown engine, so every test here would exercise the
    # unknown-engine fallback rather than the Terraform path it means to.
    ws.engine = engine

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


async def _call(
    user,
    run,
    ws,
    *,
    target="aws",
    providers=None,
    discovery="ok",
    detail="",
    issuer="https://terrapod.example.com",
    cfg=None,
):
    """Drive the batched mint.

    `target` is the single-target convenience most tests want; `providers` sends
    a list verbatim, including the empty one. `captured["claims"]` is the last
    claim set signed, and `captured["all"]` every one -- so a single-target test
    reads as it always did and a batched test can check each.
    """
    settings = MagicMock()
    settings.auth.oidc_issuer = cfg or _enabled()
    captured: dict = {"all": []}

    def _sign(claims, *, ttl_seconds):
        captured["claims"] = claims
        captured["ttl"] = ttl_seconds
        captured["all"].append(claims)
        return "signed.jwt.value"

    if providers is None:
        providers = [target] if target else []
    payload = router.CloudIdentityMintRequest(
        providers=providers, discovery=discovery, **{"discovery-detail": detail}
    )

    with (
        patch("terrapod.config.settings", settings),
        patch("terrapod.auth.oidc_signing.sign_identity_token", _sign),
        patch("terrapod.api.routers.oidc_issuer.issuer_url", return_value=issuer),
    ):
        resp = await router.mint_cloud_identity_tokens(
            payload=payload, run_id=f"run-{run.id}", user=user, db=_db(run, ws)
        )
    return resp, captured


def _tokens(resp) -> list[dict]:
    import json

    return json.loads(resp.body)["tokens"]


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

    async def test_a_request_naming_NO_PROVIDERS_is_204_not_400(self):
        """A configuration that declares no provider, reported as `ok`.

        It cannot reach a cloud, so it needs no token -- and 400 would make
        every such run FAIL when the designed behaviour is falling through to
        the agent pool's own identity exactly as before this feature existed.
        The fall-through is permanent and supported, so a request we cannot
        serve has to look like "nothing here" rather than like a fault.
        """
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg
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

    async def test_the_body_cannot_NAME_A_PHASE_or_anything_else(self):
        """The endpoint takes a body now, so the guarantee moves to its shape.

        Asserted on the model rather than behaviourally, because a behavioural
        test cannot see a field that was added but ignored today and read
        tomorrow. Two halves: the field set is exactly what discovery reports,
        and `extra="forbid"` means a runner cannot smuggle a phase past it even
        as an unknown key. `providers` names provider configurations, not
        identities -- it selects which of the run's own already-resolved entries
        to mint, and cannot introduce one.
        """
        fields = set(router.CloudIdentityMintRequest.model_fields)
        assert fields == {"providers", "discovery", "discovery_detail"}
        assert "phase" not in fields
        assert router.CloudIdentityMintRequest.model_config["extra"] == "forbid"
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            router.CloudIdentityMintRequest(providers=["aws"], phase="apply")

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
        assert _tokens(resp)[0]["target"] == "vault.eu"

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


class TestOneRequestNotOnePerTarget:
    """The restructure. The runner discovers unconditionally and sends its list
    once; the API holds the mapping, so computing the intersection is its job.

    This replaces a gate route that answered "which targets does this run mint
    for?" so the runner could skip the engine. The gate was the wrong trade: it
    made every federated run pay two hops to save one engine invocation on the
    runs that are not federated, and the engine's graph is a static walk needing
    no network, no credentials and no state.
    """

    async def test_every_discovered_provider_that_maps_gets_its_own_token(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azurerm": [AZURE]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "azurerm"],
            cfg=cfg,
        )
        assert resp.status_code == 200
        assert [t["target"] for t in _tokens(resp)] == ["aws", "azurerm"]
        assert [c["aud"] for c in captured["all"]] == [[AWS], [AZURE]]

    async def test_each_token_carries_only_its_own_audience(self):
        """The same property as the single-target case, which the batch is the
        place it could regress: one loop body reusing the previous `aud` would
        hand both targets a token the other could replay."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azurerm": [AZURE]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "azurerm"],
            cfg=cfg,
        )
        by_target = {t["target"]: t["audiences"] for t in _tokens(resp)}
        assert by_target == {"aws": [AWS], "azurerm": [AZURE]}

    async def test_only_the_intersection_is_minted(self):
        """Discovered three, the workspace maps two. Not an error either way: a
        mapping need not cover every provider a configuration uses, and a
        configuration need not use every provider in the mapping."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "vault": ["tp"]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "vault", "google"],
            cfg=cfg,
        )
        assert [t["target"] for t in _tokens(resp)] == ["aws", "vault"]

    async def test_an_ALIASED_provider_resolves_through_the_general_entry(self):
        """The bug the batch introduced, and the reason the intersection is a
        resolver call rather than a set operation.

        `vault.eu` is answered by the `vault` entry -- which is what lets an
        operator alias a provider five times without naming every alias -- so
        `set(providers) & set(snapshot)` looks equivalent and silently mints
        nothing for every aliased configuration in the fleet.
        """
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "vault": ["tp"]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "vault.eu"],
            cfg=cfg,
        )
        assert resp.status_code == 200
        by_target = {t["target"]: t["audiences"] for t in _tokens(resp)}
        assert by_target == {"aws": [AWS], "vault.eu": ["tp"]}

    async def test_a_specific_alias_entry_still_wins_in_a_batch(self):
        ws, run, cfg = _scenario(catalogue={"vault": ["general"], "vault.eu": ["eu-only"]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["vault", "vault.eu"],
            cfg=cfg,
        )
        by_target = {t["target"]: t["audiences"] for t in _tokens(resp)}
        assert by_target == {"vault": ["general"], "vault.eu": ["eu-only"]}

    async def test_nothing_in_common_is_204(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=["google"], cfg=cfg
        )
        assert resp.status_code == 204
        assert captured["all"] == []

    async def test_the_tokens_come_back_in_a_stable_order(self):
        """So the runner's log and ours can be compared, and so a test that
        reads `tokens[0]` is not a coin toss."""
        ws, run, cfg = _scenario(catalogue={"vault": ["v"], "aws": [AWS], "azurerm": [AZURE]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["vault", "aws", "azurerm"],
            cfg=cfg,
        )
        assert [t["target"] for t in _tokens(resp)] == ["aws", "azurerm", "vault"]

    async def test_a_repeated_provider_mints_once(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "aws", "aws"],
            cfg=cfg,
        )
        assert len(_tokens(resp)) == 1
        assert len(captured["all"]) == 1

    async def test_the_request_is_bounded(self):
        """The names come out of the engine's graph rather than from us, and a
        runner token is a credential a run holds rather than a reason to trust
        its body. The runner caps its list too; this caps it again."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            router.CloudIdentityMintRequest(
                providers=[f"p{i}" for i in range(router.MAX_TARGETS + 1)]
            )

    async def test_the_phase_is_the_tokens_once_for_the_whole_batch(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "vault": ["v"]})
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, providers=["aws", "vault"], cfg=cfg
        )
        import json

        assert json.loads(resp.body)["phase"] == "apply"


class TestADiscoveryTheRunnerCouldNotTrust:
    """`failed` and `unparsed` both arrive as an empty provider list, which is
    indistinguishable from a provider-less configuration -- so taking them at
    face value would be a silent fall-through to the agent pool's broader
    identity for exactly the workspaces deliberately moved off it.

    The ORDER is the whole design: a workspace holding no identity is answered
    204 before the outcome is examined, so a graph failure can never fail a run
    that was not using this feature. That is what makes reporting the outcome
    safe rather than a new way for every run in the fleet to break.
    """

    @pytest.mark.parametrize("outcome", ["failed", "unparsed"])
    async def test_it_is_refused_when_the_workspace_holds_identity(self, outcome):
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call(
                _user(run_id=str(run.id), phase="apply"),
                run,
                ws,
                providers=[],
                discovery=outcome,
                detail="tofu graph exited 1. Error: Could not load plugin",
                cfg=cfg,
            )
        assert exc.value.status_code == 409
        assert outcome in exc.value.detail
        # The runner's own words, so the operator can see which graph failed and
        # why rather than being told only that something did.
        assert "Could not load plugin" in exc.value.detail

    @pytest.mark.parametrize("outcome", ["failed", "unparsed"])
    async def test_it_is_IGNORED_when_the_workspace_holds_none(self, outcome):
        """The ordering, pinned. Examining the outcome first would fail every
        run whose engine hiccupped, in a fleet where almost no workspace uses
        this feature at all."""
        ws, run, cfg = _scenario()
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=[],
            discovery=outcome,
            cfg=cfg,
        )
        assert resp.status_code == 204

    async def test_a_disabled_issuer_short_circuits_before_everything(self):
        """An operator who has not published an issuer has not opted in, so a
        graph failure cannot possibly matter to them."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.enabled = False
        resp, _c = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=[],
            discovery="failed",
            cfg=cfg,
        )
        assert resp.status_code == 204

    async def test_an_unknown_outcome_is_rejected_by_the_model(self):
        """Not silently treated as `ok`. An outcome we do not recognise is a
        runner newer than this API, and reading it as "the list is
        authoritative" is the fall-through this whole mechanism prevents."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            router.CloudIdentityMintRequest(providers=[], discovery="probably-fine")

    async def test_the_default_outcome_is_ok(self):
        """So a body that omits it reads as "the list is authoritative" -- which
        is correct for any caller that sends a list at all, and keeps the field
        from being load-bearing for a client that does not know about it."""
        assert router.CloudIdentityMintRequest(providers=["aws"]).discovery == "ok"


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

    async def test_every_target_in_one_batch_is_recorded(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azure": [AZURE]})
        run.oidc_minted_targets = []
        await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws", "azure"], cfg=cfg
        )
        assert run.oidc_minted_targets == ["aws", "azure"]

    async def test_a_second_batch_accumulates_rather_than_replacing(self):
        """The apply phase re-mints what the plan did, and may use more. The
        confirm check reads the union, so the plan's identities must survive."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azure": [AZURE]})
        run.oidc_minted_targets = []
        await _call(_user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws"], cfg=cfg)
        await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, providers=["aws", "azure"], cfg=cfg
        )
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

    async def test_a_batch_naming_a_target_twice_records_it_once(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        run.oidc_minted_targets = []
        await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws", "aws"], cfg=cfg
        )
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


class TestATargetCannotEscapeItsOwnDirectory:
    """A target is echoed back and the runner joins it into
    `<token dir>/<target>/token`, so an unchecked one is a filesystem write
    primitive, not a label.

    `providers`'s `max_length` bounds the LIST and never an item, and
    `audiences_for_target` splits on the FIRST dot — so `aws./../vault`
    resolves through an ordinary `aws` entry that any workspace might have.
    Reachable by anything holding this run's token, which includes the
    workspace's own configuration and a fork-PR speculative plan.
    """

    @pytest.mark.parametrize(
        "target",
        [
            "aws./../vault",  # resolves via `aws`, lands on the `vault` path
            "aws./../../../tmp/x",  # leaves the token directory entirely
            "/etc/evil",  # absolute, ignores the directory altogether
            "foo/bar",  # no dot at all, so the one-dot rule never sees it
            "a\\b",
        ],
    )
    async def test_a_path_significant_target_is_refused(self, target):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, target=target, cfg=cfg)
        assert exc.value.status_code == 400
        # The repr, because that is what the message carries — a backslash in a
        # target is escaped there and the raw string would not match.
        assert repr(target) in exc.value.detail

    @pytest.mark.parametrize("target", ["aws", "aws.west", "vault.eu"])
    async def test_an_ordinary_target_still_mints(self, target):
        """The guard must not narrow the documented `provider[.alias]` form."""
        ws, run, cfg = _scenario(catalogue={target.split(".")[0]: [AWS]})
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target=target, cfg=cfg
        )
        assert resp.status_code == 200


class TestAnEngineThatCannotDiscover:
    """A non-discovering engine is minted the whole resolved mapping (#2006).

    Terraform's runner enumerates its provider configurations with a static
    `graph` walk and this route intersects that list with what the workspace
    holds. Pulumi's cannot — a Pulumi program is arbitrary code whose provider
    instances are built at runtime, and the thing that would run it is what
    needs the credentials — so narrowing would mean guessing, and a guess that
    comes out short fails inside the engine at the cloud's token exchange.

    The engine is read off the workspace ROW, never from the request: the runner
    image does not ship `terrapod.engines`, and a runner's claim about which
    engine it is would be the runner's rather than the platform's.
    """

    async def test_everything_in_the_mapping_is_minted(self):
        """The runner asked for nothing and gets all three."""
        ws, run, cfg = _scenario(
            catalogue={"vault": ["https://vault.example.com"]},
            override={"aws": ["sts.example.com"], "gcp": ["//iam.example/p"]},
            engine="pulumi",
        )
        resp, cap = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg
        )
        assert resp.status_code == 200
        assert sorted(t["target"] for t in _tokens(resp)) == ["aws", "gcp", "vault"]
        # Each carries only its own audiences — the one-token-per-target rule is
        # not relaxed just because the set was not discovered.
        by_target = {t["target"]: t["audiences"] for t in _tokens(resp)}
        assert by_target["aws"] == ["sts.example.com"]
        assert by_target["vault"] == ["https://vault.example.com"]
        assert len(cap["all"]) == 3

    async def test_a_terraform_workspace_still_intersects(self):
        """The regression guard. An empty list from a DISCOVERING engine still
        means "this configuration declares no provider" and answers 204 — the
        meaning of the same request must not have changed for Terraform."""
        ws, run, cfg = _scenario(
            override={"aws": ["sts.example.com"], "gcp": ["//iam.example/p"]},
            engine="terraform",
        )
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg
        )
        assert resp.status_code == 204

    async def test_only_the_discovered_subset_for_terraform(self):
        ws, run, cfg = _scenario(
            override={"aws": ["sts.example.com"], "gcp": ["//iam.example/p"]},
            engine="terraform",
        )
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws"], cfg=cfg
        )
        assert [t["target"] for t in _tokens(resp)] == ["aws"]

    async def test_a_bad_discovery_outcome_is_not_held_against_it(self):
        """`failed`/`unparsed` describe reading a graph, and this engine never
        read one — so refusing on the outcome would refuse every Pulumi run.

        The runner sends `ok`; this pins that the route does not depend on it,
        so a lagging or future runner reporting something else still works.
        """
        ws, run, cfg = _scenario(override={"aws": ["sts.example.com"]}, engine="pulumi")
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=[],
            discovery="failed",
            detail="pulumi has no graph subcommand",
            cfg=cfg,
        )
        assert resp.status_code == 200
        assert [t["target"] for t in _tokens(resp)] == ["aws"]

    async def test_a_discovering_engine_is_still_refused_on_a_bad_outcome(self):
        """The other side of the gate, so narrowing it to `discovers` did not
        quietly disable the refusal it was introduced for."""
        ws, run, cfg = _scenario(override={"aws": ["sts.example.com"]}, engine="terraform")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as err:
            await _call(
                _user(run_id=str(run.id), phase="plan"),
                run,
                ws,
                providers=[],
                discovery="unparsed",
                cfg=cfg,
            )
        assert err.value.status_code == 409

    async def test_a_workspace_holding_nothing_is_unaffected(self):
        """204 before anything else, as for every engine: a workspace that
        configured none of this cannot be failed by it."""
        ws, run, cfg = _scenario(engine="pulumi")
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg
        )
        assert resp.status_code == 204

    async def test_over_the_cap_is_refused_rather_than_truncated(self):
        """The cap stops being slack and becomes the real bound.

        Truncating would deliver a token set that looks complete and is not —
        the missing one surfaces inside the engine at the cloud's token
        exchange, naming neither the file nor the reason.
        """
        many = {f"t{i}": [f"aud-{i}"] for i in range(router.MAX_TARGETS + 1)}
        ws, run, cfg = _scenario(override=many, engine="pulumi")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as err:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg)
        assert err.value.status_code == 409
        assert str(router.MAX_TARGETS) in err.value.detail
        assert "oidc_audiences" in err.value.detail

    async def test_exactly_the_cap_is_served(self):
        """The boundary, so the refusal is off-by-one in the safe direction."""
        many = {f"t{i}": [f"aud-{i}"] for i in range(router.MAX_TARGETS)}
        ws, run, cfg = _scenario(override=many, engine="pulumi")
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg
        )
        assert resp.status_code == 200
        assert len(_tokens(resp)) == router.MAX_TARGETS

    async def test_a_provider_list_is_ignored_not_honoured(self):
        """A future runner sending a list it cannot have discovered must not
        narrow the mint — otherwise a runner bug silently withholds an identity
        the program needs."""
        ws, run, cfg = _scenario(
            override={"aws": ["sts.example.com"], "gcp": ["//iam.example/p"]}, engine="pulumi"
        )
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws"], cfg=cfg
        )
        assert sorted(t["target"] for t in _tokens(resp)) == ["aws", "gcp"]

    async def test_a_stored_target_that_could_escape_its_directory_is_refused(self):
        """Defence in depth on the operator's own stored data.

        The write side validates these, so this is reachable only for a key
        written before that guard existed — but the target is echoed back and
        the runner joins it into a path, so it is checked here too, and as a 422
        about their configuration rather than a 400 about the request.
        """
        ws, run, cfg = _scenario(override={"../../etc/passwd": ["x"]}, engine="pulumi")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as err:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg)
        assert err.value.status_code == 422
        assert "oidc_audiences" in err.value.detail

    async def test_a_configuration_that_moved_still_refuses(self):
        """The staleness check is unchanged, and for Pulumi it covers the whole
        mapping — correctly, because every entry is one this run would present."""
        ws, run, cfg = _scenario(
            override={"aws": ["sts.example.com"]},
            snapshot={"aws": ["sts.old.example.com"]},
            engine="pulumi",
        )
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as err:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, providers=[], cfg=cfg)
        assert err.value.status_code == 409
        assert "'aws'" in err.value.detail

    async def test_the_phase_still_comes_from_the_token(self):
        """Not weakened by the engine branch: a plan-phase runner cannot obtain
        the apply identity however its targets were chosen."""
        ws, run, cfg = _scenario(override={"aws": ["sts.example.com"]}, engine="pulumi")
        _, cap = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, providers=[], cfg=cfg
        )
        assert cap["claims"]["phase"] == "apply"
        assert cap["claims"]["sub"].endswith(":phase:apply")
