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

import textwrap
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import cloud_identity as router
from terrapod.services import cloud_identity_resolver, workspace_settings

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
    # Likewise a real string: `_subject` compares it to "name", and a MagicMock
    # would take the id branch by accident rather than by configuration -- so the
    # default-path tests would pass without the default ever being modelled.
    cfg.subject_format = over.get("subject_format", "name-and-id")
    # A real bool for the same reason: the mint refuses an unphased token when
    # this is set, and a MagicMock is truthy -- so every unphased-token test
    # would be asserting the refusal rather than the fall-through.
    cfg.require_phase = over.get("require_phase", False)
    cfg.require_assigned_pool = over.get("require_assigned_pool", False)
    return cfg


def _scenario(*, catalogue=None, override=None, snapshot=None, name="dns-prod", pool=None):
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
    # A real None, not a MagicMock: the mint looks the pool up only when this is
    # set, and a MagicMock is truthy -- so every test would make a pool query the
    # fixture then answered None for, which is the shape of a locally executed
    # run arriving by accident rather than by configuration. `pool` is the
    # AgentPool the run was claimed by, which is what `run.pool_id` holds once a
    # listener has taken it.
    run.pool_id = pool.id if pool is not None else None
    run._pool = pool

    return ws, run, _enabled(audiences=catalogue)


def _pool(name="prod-eu"):
    pool = MagicMock()
    pool.id = uuid.uuid4()
    pool.name = name
    return pool


def _db(run, ws, *, looked_up: list | None = None, pool_still_assigned: bool = True):
    db = MagicMock()
    # The membership query behind `require_assigned_pool`. A real value, because
    # the route treats None as "detached" -- a MagicMock would read as assigned
    # and the refusal could never be reached.
    db.scalar = AsyncMock(return_value=run.pool_id if pool_still_assigned else None)

    async def _get(model, pk):
        from terrapod.db.models import AgentPool, Run, Workspace

        if looked_up is not None:
            looked_up.append(model)
        if model is Run:
            return run
        if model is Workspace:
            return ws
        if model is AgentPool:
            # Mirrors SQLAlchemy: a None primary key does NOT short-circuit. It
            # warns `fully NULL primary key identity cannot load any object` and
            # still issues the query -- "may raise an error in a future release".
            # So returning None here would hide the very guard that stops a
            # pointless round-trip on every locally executed run.
            assert pk is not None, (
                "the mint looked up an agent pool with a null id; SQLAlchemy "
                "queries anyway and warns, and is documented to start raising"
            )
            return getattr(run, "_pool", None)
        return None

    db.get = AsyncMock(side_effect=_get)
    # The mint WRITES now — it records the target it served, which is what the
    # confirm-time staleness check is scoped to. A MagicMock here is not
    # awaitable, so this fixture has to match production rather than the route's
    # earlier read-only shape.
    db.commit = AsyncMock()
    # The mint re-reads the run under a row lock before appending to
    # `oidc_minted_targets`, so the fixture has to model that. A no-op is right:
    # there is no second writer here, and the row object is already the one the
    # route holds.
    db.refresh = AsyncMock()
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
    looked_up=None,
    pool_still_assigned=True,
    status=None,
):
    """Drive the batched mint.

    `target` is the single-target convenience most tests want; `providers` sends
    a list verbatim, including the empty one. `captured["claims"]` is the last
    claim set signed, and `captured["all"]` every one -- so a single-target test
    reads as it always did and a batched test can check each.
    """
    # The run's status is DERIVED from the token's phase unless a test pins it.
    #
    # The mint refuses a run that is not executing the phase its token names
    # (GHSA-p9jh), so a MagicMock status fails every test for the wrong reason.
    # Deriving it models production -- `claim_next_run` moves `queued ->
    # planning` and `confirmed -> applying` atomically as it hands the run over,
    # so a runner holding a plan-phase token is on a `planning` run by
    # construction. `status=` is for the tests that are ABOUT the refusal.
    if status is not None:
        run.status = status
    elif not isinstance(run.status, str):
        run.status = "applying" if user.run_phase == "apply" else "planning"

    settings = MagicMock()
    settings.auth.oidc_issuer = cfg or _enabled()
    captured: dict = {"all": []}

    def _issuer() -> str:
        captured["issuer_url_returned"] = issuer
        return issuer

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
        # Recorded rather than only patched, so a test can assert the `iss`
        # claim equals what THIS FUNCTION returned during the call -- three
        # values have to agree and a literal on both sides of the comparison
        # would pass while the mint read some other source.
        patch("terrapod.api.routers.oidc_issuer.issuer_url", side_effect=_issuer),
    ):
        resp = await router.mint_cloud_identity_tokens(
            payload=payload,
            run_id=f"run-{run.id}",
            user=user,
            db=_db(run, ws, looked_up=looked_up, pool_still_assigned=pool_still_assigned),
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


class TestTheAgentPoolIsClaimed:
    """Which pool executed a run decides WHERE its identity can be minted, and
    the token said nothing about it.

    Moving a workspace to another pool -- or adding one, which `pool:assign` on
    the DESTINATION pool allows -- moves that, and neither the cloud nor an
    auditor could tell from the token that it had happened. The claim is the
    pool the run was actually CLAIMED by (`run.pool_id` is rewritten on claim),
    not the workspace's first choice, because the first choice is not what ran.
    """

    async def test_the_pool_name_and_id_are_both_claimed(self):
        """Both, for the same reason `workspace` and `workspace_id` are both
        there: the name is what a human reads in a policy and the id is what
        survives a rename."""
        pool = _pool("prod-eu")
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, pool=pool)
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["agent_pool"] == "prod-eu"
        assert captured["claims"]["agent_pool_id"] == str(pool.id)

    async def test_a_run_with_no_pool_omits_both_rather_than_claiming_null(self):
        """A locally executed workspace has no pool. A claim present with no
        value is worse than absent: a Rego rule testing
        `input.agent_pool == "prod"` fails either way, while one testing for
        presence would pass on a null."""
        from terrapod.db.models import AgentPool

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        looked_up: list = []
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            target="aws",
            cfg=cfg,
            looked_up=looked_up,
        )
        assert "agent_pool" not in captured["claims"]
        assert "agent_pool_id" not in captured["claims"]
        # And it must not ASK. `db.get(Model, None)` is not a short-circuit in
        # SQLAlchemy -- it warns and queries anyway -- so without the `if
        # run.pool_id` guard this is a wasted round-trip on every locally
        # executed run, and a future SQLAlchemy is documented to raise.
        assert AgentPool not in looked_up, (
            "the mint queried for an agent pool on a run that has none"
        )

    async def test_every_target_in_a_batch_carries_it(self):
        """The claim is built per target, so a batched mint is where a per-target
        construction drops one."""
        pool = _pool("shared")
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azurerm": [AZURE]}, pool=pool)
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            providers=["aws", "azurerm"],
            cfg=cfg,
        )
        # `captured["all"]` is every claim set signed, not only the last -- which
        # is the only way to see a per-target construction that drops one.
        assert len(captured["all"]) == 2
        assert [c["agent_pool"] for c in captured["all"]] == ["shared", "shared"]
        assert {c["agent_pool_id"] for c in captured["all"]} == {str(pool.id)}


class TestRequireAssignedPoolRefusesADetachedPool:
    """`require_assigned_pool` (#2040).

    A run's candidate pools are snapshotted at creation, so detaching a pool
    from the workspace afterwards leaves that run claimable by a pool the
    workspace no longer has -- and which pool runs a workspace is which
    listeners its federated credential is reachable from.

    It asks about MEMBERSHIP, not about a primary pool: there is none. The pool
    set is flat, `ordinal` is display order with no dispatch meaning, and a run
    is offered to every pool at once so which claims it is a race. A first-pool
    rule would refuse at random on any multi-pool workspace, which is why this
    one does not exist.
    """

    async def test_a_detached_pool_is_refused_when_it_is_set(self):
        pool = _pool("retired-pool")
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, pool=pool)
        cfg.require_assigned_pool = True
        with pytest.raises(HTTPException) as exc:
            await _call(
                _user(run_id=str(run.id), phase="apply"),
                run,
                ws,
                target="aws",
                cfg=cfg,
                pool_still_assigned=False,
            )
        assert exc.value.status_code == 403
        assert "require_assigned_pool" in exc.value.detail
        assert "retired-pool" in exc.value.detail, "the operator needs to know WHICH pool"

    async def test_a_STILL_ASSIGNED_pool_mints_normally(self):
        """The contrast, and the thing that makes this safe to turn on: every
        pool in the set is a legitimate executor, so an ordinary multi-pool run
        must pass."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, pool=_pool("prod-eu"))
        cfg.require_assigned_pool = True
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["agent_pool"] == "prod-eu"

    async def test_a_detached_pool_mints_when_it_is_NOT_set(self):
        """The default. The window is narrow and the refusal fails a run, so an
        operator opts in rather than inheriting it."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, pool=_pool("retired-pool"))
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            target="aws",
            cfg=cfg,
            pool_still_assigned=False,
        )
        assert captured["claims"]["agent_pool"] == "retired-pool"

    async def test_a_run_with_no_pool_is_not_refused(self):
        """A locally executed workspace has no pool at all, so there is nothing
        to be detached -- and refusing it would make this setting mean "agent
        execution only", which it does not say."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.require_assigned_pool = True
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            target="aws",
            cfg=cfg,
            pool_still_assigned=False,
        )
        assert "agent_pool" not in captured["claims"]


class TestRequirePhaseRefusesAnUnphasedToken:
    """`require_phase` closes off the unphased form entirely (#2041).

    An unphased runner token exists only for a listener predating the phase
    claim. It mints an identity with no `phase` claim and an unphased subject,
    which is a form every trust policy then has to remember to exclude -- and a
    wildcard that forgets is how a plan-phase run reaches an apply-phase role.

    Off by default, because an upgrade must not break a lagging listener.
    """

    async def test_an_unphased_token_is_refused_when_it_is_set(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.require_phase = True
        with pytest.raises(HTTPException) as exc:
            await _call(_user(run_id=str(run.id)), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 403
        assert "require_phase" in exc.value.detail

    async def test_a_PHASED_token_is_unaffected(self):
        """The contrast. A refusal that caught phased tokens too would break
        every run on a deployment that set this, which is the opposite of what
        it is for."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.require_phase = True
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["phase"] == "apply"

    async def test_an_unphased_token_still_mints_when_it_is_NOT_set(self):
        """The default, and it has to stay working: a listener predating the
        phase claim is the only thing that produces an unphased token, and an
        upgrade must not strand it."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        _, captured = await _call(_user(run_id=str(run.id)), run, ws, target="aws", cfg=cfg)
        assert "phase" not in captured["claims"]


class TestTheSubjectCarriesTheWorkspaceId:
    """`sub` is the only thing an AWS or Azure trust policy can bind, and a
    workspace NAME is mutable and reusable -- any authenticated user can create
    one with a free name, an admin can rename, and a delete frees the name. So a
    name-only subject is satisfied by whatever workspace holds that name when it
    runs. The id is immutable, which is the whole point of putting it there.
    """

    async def test_two_workspaces_with_THE_SAME_NAME_get_different_subjects(self):
        """The property, stated the way it matters. Asserting the literal string
        would pass just as well with the id omitted from one of them."""
        ws_a, run_a, cfg = _scenario(catalogue={"aws": [AWS]}, name="dns-prod")
        ws_b, run_b, _ = _scenario(catalogue={"aws": [AWS]}, name="dns-prod")
        assert ws_a.name == ws_b.name
        assert ws_a.id != ws_b.id

        _, cap_a = await _call(
            _user(run_id=str(run_a.id), phase="apply"), run_a, ws_a, target="aws", cfg=cfg
        )
        _, cap_b = await _call(
            _user(run_id=str(run_b.id), phase="apply"), run_b, ws_b, target="aws", cfg=cfg
        )
        assert cap_a["claims"]["sub"] != cap_b["claims"]["sub"], (
            "a workspace that takes a trusted name must not inherit its subject"
        )

    async def test_a_name_cannot_forge_the_id_segment(self):
        """Safe to concatenate only because the name grammar
        (`^[a-zA-Z0-9][a-zA-Z0-9_-]*$`) admits neither `@` nor `:`. If that ever
        relaxes, the composite stops parsing unambiguously -- so pin it here,
        beside the thing that depends on it."""
        from terrapod.services.workspace_name import validate_workspace_name

        for forged in ("dns-prod@11111111-1111-1111-1111-111111111111", "dns-prod:phase:apply"):
            with pytest.raises(ValueError):
                validate_workspace_name(forged)

    async def test_the_legacy_name_only_form_is_still_reachable(self):
        """Kept only so a trust policy written in the three days the name-only
        form was the default can be migrated rather than broken at the upgrade.
        It goes in 2.0."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        cfg.subject_format = "name"
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["sub"] == "workspace:dns-prod:phase:apply"


class TestTheIdentifyingClaimsCannotBeDrivenFromTheREQUEST:
    """`run_id`, `workspace` and `workspace_id` come from the server's own rows.

    They are what a trust policy binds, so a request field that reached any of
    them would let a run mint an identity for a workspace it is not. The body is
    `extra="forbid"`, which is the structural half; these pin that it stays
    structural and that the values come from the run and workspace the SERVER
    resolved from the token's run id.
    """

    async def test_an_unknown_body_field_is_refused_outright(self):
        """`extra="forbid"`, so there is no field to smuggle one in through.
        Asserted here rather than left to the model, because relaxing it to
        `ignore` is a one-word edit that no behavioural test would notice."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            router.CloudIdentityMintRequest(providers=["aws"], workspace_id="ws-evil")

    async def test_the_model_forbids_extras_by_configuration(self):
        assert router.CloudIdentityMintRequest.model_config["extra"] == "forbid"

    async def test_the_claims_come_from_the_SERVER_rows(self):
        """Not from anything the caller sent. The runner names only providers and
        a discovery outcome; the identifying claims are read off the run the
        token is scoped to and the workspace that run belongs to."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, name="finance-prod")
        _, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        claims = captured["claims"]
        assert claims["workspace"] == "finance-prod"
        assert claims["workspace_id"] == str(ws.id)
        assert claims["run_id"] == str(run.id)

    async def test_the_run_id_claim_is_the_RESOLVED_run_not_the_token_string(self):
        """The token carries a bare uuid and the path may carry `run-<uuid>`;
        both are normalised before the scope check. So the claim has to be the
        resolved run's own id, or a `run-` prefixed caller would get a `run_id`
        claim no cloud audit log could match back to a Terrapod run."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        _, captured = await _call(
            _user(run_id=f"run-{run.id}", phase="apply"), run, ws, target="aws", cfg=cfg
        )
        assert captured["claims"]["run_id"] == str(run.id)
        assert not captured["claims"]["run_id"].startswith("run-")


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
        assert captured["claims"]["sub"] == f"workspace:dns-prod@{ws.id}:phase:{phase}"

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
        assert captured["claims"]["sub"] == f"workspace:dns-prod@{ws.id}"


class TestTheClaimSet:
    async def test_it_carries_what_a_trust_policy_conditions_on(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        resp, captured = await _call(
            _user(run_id=str(run.id), phase="apply"), run, ws, target="aws", cfg=cfg
        )
        claims = captured["claims"]

        # Against `issuer_url()`'s own answer, not a literal: three values have
        # to agree (this claim, the discovery document's `issuer`, its
        # `jwks_uri`) and a hardcoded string here would pass while the mint read
        # a different source. Read from the module the mint imports it from --
        # the import is function-local, so that attribute is what it resolves at
        # call time, and it is the one `_call` patches.
        assert claims["iss"] == captured["issuer_url_returned"]
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
    @pytest.mark.parametrize("method", ["session", "api_token"])
    async def test_every_non_runner_auth_method_is_refused(self, method):
        """Runner protocol only. A person has no business minting a run's cloud
        identity, and `require_runner_for_run` is what says so -- it compares
        `auth_method` to the single literal `runner_token`, so EVERY other method
        has to be refused rather than only the one a test happened to pick.

        `api_token` is the one that matters, and it was untested. A session is a
        person at a browser; an API token is automation, which is exactly the
        shape of caller that would plausibly try this -- and it is the long-lived
        credential, where a session expires in twelve hours.
        """
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call(_user(method=method), run, ws, target="aws", cfg=cfg)
        assert exc.value.status_code == 403
        assert "Runner token required" in exc.value.detail

    async def test_an_api_token_SCOPED_TO_THE_RUN_is_still_refused(self):
        """The subtler half: refusal is on the METHOD, not on whether the caller
        named the right run. An API token that somehow carried a matching
        `run_id` must not pass -- otherwise a caller who can read a run id can
        mint its cloud identity."""
        from fastapi import HTTPException

        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        with pytest.raises(HTTPException) as exc:
            await _call(
                _user(method="api_token", run_id=str(run.id), phase="apply"),
                run,
                ws,
                target="aws",
                cfg=cfg,
            )
        assert exc.value.status_code == 403
        assert "Runner token required" in exc.value.detail

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

    async def test_a_runner_token_cannot_read_the_catalogue(self):
        """Supersedes a test that asserted 200 here.

        Its reasoning was "it does not special-case the runner, and does not
        need to — the runner never calls it". The second half still holds and is
        why nothing legitimate breaks: the mint already returns this run's own
        resolved audiences, which is all the credential phase writes. The first
        half was the wrong question. What the legitimate runner calls says
        nothing about what anything HOLDING a runner token can call, and the plan
        Job runs the workspace's own HCL — an `external` data source, a
        `pre_init` hook or a third-party module can read `TP_AUTH_TOKEN` out of
        the environment.

        What it would read is deployment-wide topology: every federation target
        every workspace in the deployment uses. The discovery document
        deliberately publishes no audiences for exactly that reason, so serving
        them one tier up to a runner token gave the disclosure straight back.
        """
        with pytest.raises(HTTPException) as e:
            await _call_defaults(_user(run_id="r", phase="plan"), cfg=_enabled(audiences={}))
        assert e.value.status_code == 403

    async def test_a_session_user_still_reads_it(self):
        """The negative path: the refusal must be scoped to runner tokens, not
        applied to the practitioner the route exists for."""
        resp = await _call_defaults(_user(method="session"), cfg=_enabled(audiences={}))
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
        confirm check reads the union, so the plan's identities must survive.

        The run's status is ADVANCED between the two calls, because that is what
        happens: a run plans, is confirmed, and is claimed into `applying`. It
        used to be left alone, which only passed because nothing read it -- and
        now that the mint checks the phase against the state, modelling the
        sequence is the honest fixture.
        """
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "azure": [AZURE]})
        run.oidc_minted_targets = []
        await _call(_user(run_id=str(run.id), phase="plan"), run, ws, providers=["aws"], cfg=cfg)
        await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            providers=["aws", "azure"],
            cfg=cfg,
            status="applying",
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


class TestOnlyARunEXECUTINGThePhaseCanMint:
    """A runner token is good until it EXPIRES -- run state is not checked when
    it is verified, and the default TTL is an hour.

    That is an inherited property of runner tokens rather than something new,
    but this is the first endpoint that converts one into CLOUD credentials, so
    the missing liveness check costs more here. Code in the plan pod that
    exfiltrates the token could keep POSTing after the run finished, drawing
    fresh short-lived credentials for the workspace's identity, from anywhere,
    for the remainder of the hour. `debug_mode`'s lingering pods widen the
    window further.

    **This class used to be `TestAFinishedRunCannotMint`, and the premise it
    tested was wrong** (GHSA-p9jh). The check refused `TERMINAL_STATES`, and a
    plan-only run never reaches one: it finishes in `planned` and stays there.
    Speculative pull-request plans, module-impact plans and drift runs are ALL
    plan-only -- and are exactly the runs executing the least-trusted code, an
    unmerged branch. So the one class of run the guard most needed to cover was
    the one it could never fire on, and every test here passed throughout.

    The question is "is this run executing the phase its token names", which the
    terminal set cannot express.
    """

    @pytest.mark.parametrize("status", ["applied", "errored", "discarded", "canceled"])
    async def test_a_terminal_run_is_refused(self, status):
        """Still refused -- a terminal run executes nothing -- but now as a
        consequence of the rule rather than as the rule itself."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        run.status = status
        with pytest.raises(HTTPException) as e:
            await _call(_user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg, target="aws")
        assert e.value.status_code == 409
        assert status in e.value.detail

    async def test_a_PLANNED_run_is_refused_which_the_terminal_check_could_not_do(self):
        """The finding, stated as the one case that distinguishes the two rules.

        `planned` is in no terminal set, so the old check let this through --
        and it is where every plan-only run comes to rest, which is where a
        pull-request plan's exfiltrated token lived.
        """
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        with pytest.raises(HTTPException) as e:
            await _call(
                _user(run_id=str(run.id), phase="plan"),
                run,
                ws,
                cfg=cfg,
                target="aws",
                status="planned",
            )
        assert e.value.status_code == 409
        assert "plan-only" in e.value.detail, "the message must name why `planned` is the case"

    @pytest.mark.parametrize("status", ["queued", "pending"])
    async def test_a_run_not_yet_executing_is_refused_too(self, status):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        with pytest.raises(HTTPException) as e:
            await _call(
                _user(run_id=str(run.id), phase="plan"),
                run,
                ws,
                cfg=cfg,
                target="aws",
                status=status,
            )
        assert e.value.status_code == 409

    async def test_a_PLAN_phase_token_cannot_mint_on_an_APPLYING_run(self):
        """The phases are checked against each other, not merely against
        liveness. A plan-phase token on an applying run is a token outliving its
        own phase, which is the same residual-lifetime problem one state along."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        with pytest.raises(HTTPException) as e:
            await _call(
                _user(run_id=str(run.id), phase="plan"),
                run,
                ws,
                cfg=cfg,
                target="aws",
                status="applying",
            )
        assert e.value.status_code == 409

    async def test_a_live_run_still_mints(self):
        """The negative path. A refusal that caught a run actually executing its
        phase would break every federated run in the estate."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        run.status = "planning"
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, cfg=cfg, target="aws"
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("status", ["confirmed", "applying"])
    async def test_an_apply_phase_token_mints_in_both_apply_states(self, status):
        """`confirmed` is kept on purpose; see `LIVE_STATES_FOR_PHASE`. A patch
        must not fail every apply to buy a window in which no Job has launched."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]}, override={"aws": [AWS]})
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="apply"),
            run,
            ws,
            cfg=cfg,
            target="aws",
            status=status,
        )
        assert resp.status_code == 200


class TestATargetNameIsBounded:
    """`providers`' `max_length` bounds the LIST; this bounds an ITEM.

    The write path caps a provider name at `MAX_OIDC_TARGET_LEN`, so no
    catalogue key can be longer -- but a longer name still RESOLVES, because
    `audiences_for_target` splits on the first dot and falls back to the bare
    provider type. So `aws.` + 64KB answers through an ordinary `aws` entry,
    and without a cap that name is echoed back to the runner, joined into a
    path, written into `oidc_minted_targets` for ever and named in a log line.
    Anything holding this run's token can send it, 100 at a time.
    """

    async def test_a_name_longer_than_the_write_path_allows_is_refused(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        from fastapi import HTTPException

        long_alias = "aws." + ("x" * workspace_settings.MAX_OIDC_TARGET_LEN)
        with pytest.raises(HTTPException) as exc:
            await _call(
                _user(run_id=str(run.id), phase="plan"), run, ws, target=long_alias, cfg=cfg
            )
        assert exc.value.status_code == 400
        assert str(workspace_settings.MAX_OIDC_TARGET_LEN) in exc.value.detail

    async def test_the_refusal_does_not_echo_the_name_back(self):
        """The name is attacker-chosen and may be 64KB. The sibling path-safety
        refusal echoes it deliberately (it is short and naming it is the point);
        this one must not, or the response is the amplification."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        from fastapi import HTTPException

        junk = "z" * 4096
        with pytest.raises(HTTPException) as exc:
            await _call(
                _user(run_id=str(run.id), phase="plan"), run, ws, target=f"aws.{junk}", cfg=cfg
            )
        assert junk not in exc.value.detail
        assert len(exc.value.detail) < 200

    async def test_a_name_at_the_limit_still_mints(self):
        """The negative path: the cap must not refuse a name the write path
        would have accepted, or a legitimate long alias stops working."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        at_limit = "aws." + ("x" * (workspace_settings.MAX_OIDC_TARGET_LEN - 4))
        assert len(at_limit) == workspace_settings.MAX_OIDC_TARGET_LEN
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target=at_limit, cfg=cfg
        )
        assert resp.status_code == 200


class TestTheMintedRecordIsBounded:
    """`oidc_minted_targets` accumulates ACROSS requests, so without a cap it
    grows without bound: the runner retries on 5xx and asks once per phase, and
    each request may name 100 targets it has not named before.

    It is truncated rather than refused, because by this point the tokens are
    signed and on their way back -- failing here would hand the runner
    credentials the record disclaims. A short record NARROWS the confirm-time
    staleness check rather than widening it, which is the safe direction.
    """

    async def test_the_record_stops_at_the_request_cap(self):
        ws, run, cfg = _scenario(catalogue={"aws": [AWS]})
        # Already at the ceiling from earlier requests.
        run.oidc_minted_targets = [f"aws.pre{i}" for i in range(router.MAX_TARGETS)]
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"), run, ws, target="aws.new", cfg=cfg
        )
        assert resp.status_code == 200
        assert len(run.oidc_minted_targets) == router.MAX_TARGETS

    async def test_an_ordinary_run_is_not_truncated(self):
        """So the test above cannot pass against a cap that truncates always."""
        ws, run, cfg = _scenario(catalogue={"aws": [AWS], "vault": ["https://v"]})
        run.oidc_minted_targets = []
        resp, _ = await _call(
            _user(run_id=str(run.id), phase="plan"),
            run,
            ws,
            providers=["aws", "vault"],
            cfg=cfg,
        )
        assert resp.status_code == 200
        assert sorted(run.oidc_minted_targets) == ["aws", "vault"]


class TestTheSignaturesDoNotRunOnTheEventLoop:
    """Rule 13, and a POSITIONAL property, so it needs a positional gate.

    One RS256 signature is ~1ms (measured) and the handler signs once per
    target, so a full request is ~100ms of blocking CPU -- on the event loop
    every tenant shares, requested by anything holding one run's token.
    `rotate_signing_key` already hands its keygen to a thread citing this rule
    by name; this handler did not, and no behavioural test can see the
    difference because the result is identical either way.
    """

    def test_the_signing_call_is_inside_a_to_thread_and_not_in_the_handler_body(self):
        import ast
        import inspect

        src = inspect.getsource(router.mint_cloud_identity_tokens)
        tree = ast.parse(textwrap.dedent(src))

        # Every `sign_identity_token(...)` call, and whether a nested plain
        # `def` encloses it. A call in the async handler's own body is the
        # defect; a call inside the function handed to `to_thread` is the fix.
        nested: list[ast.FunctionDef] = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        ]
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "sign_identity_token"
        ]
        assert calls, "the handler no longer signs anything -- this gate lost its subject"
        for call in calls:
            enclosed = any(any(call is c for c in ast.walk(fn)) for fn in nested)
            assert enclosed, (
                "sign_identity_token runs directly in the async handler body, so "
                "every signature blocks the event loop (rule 13). It belongs in "
                "the function handed to asyncio.to_thread."
            )

        awaited = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and n.value.func.attr == "to_thread"
        ]
        assert awaited, "nothing is handed to asyncio.to_thread any more"
