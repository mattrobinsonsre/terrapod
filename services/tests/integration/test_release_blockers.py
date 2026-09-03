"""Regression tests for the v1.6.0 release blockers.

Each of these fails on the code as first written. They are grouped here rather
than scattered because they share a cause worth naming: every one lives in a
write path, an upgrade path, a failure path, or an execution mode that the
original tests did not exercise. The features' happy paths were well covered
and none of these was visible to any contract snapshot.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    VariableSet,
    VariableSetWorkspace,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.services import variable_service
from tests.integration.conftest import AUTH, admin_user, set_auth, set_listener_auth

pytestmark = pytest.mark.integration


async def _pool_with_listener(client, tag: str):
    """A real pool + join token + joined listener via the actual endpoints."""
    resp = await client.post(
        "/api/terrapod/v1/agent-pools",
        json={"data": {"type": "agent-pools", "attributes": {"name": f"blk-pool-{tag}"}}},
        headers=AUTH,
    )
    pool_id = resp.json()["data"]["id"]
    tok = await client.post(
        f"/api/terrapod/v1/agent-pools/{pool_id}/tokens",
        json={"data": {"attributes": {"description": "t"}}},
        headers=AUTH,
    )
    joined = await client.post(
        f"/api/terrapod/v1/agent-pools/{pool_id}/listeners/join",
        json={"join_token": tok.json()["data"]["attributes"]["token"], "name": f"l-{tag}"},
    )
    return pool_id, joined.json()["data"]["listener_id"]


async def _agent_run_with_vault_var(client, tag: str, pool_id: str):
    """An agent workspace with a vault variable, a CV, and a queued run."""
    from terrapod.services import pool_set, run_service

    ws_id = await _ws(client, f"blk-{tag}", **{"execution-mode": "agent"})
    ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
    await client.post(
        f"/api/v2/workspaces/{ws_id}/vars",
        json={
            "data": {
                "type": "vars",
                "attributes": {
                    "key": "TOK",
                    "category": "env",
                    "value-source": "vault",
                    "value": ref,
                },
            }
        },
        headers=AUTH,
    )
    async with get_db_session() as db:
        ws = (
            await db.execute(
                select(Workspace).where(Workspace.id == uuid.UUID(ws_id.removeprefix("ws-")))
            )
        ).scalar_one()
        pool_set.set_workspace_pools(ws, [uuid.UUID(pool_id.removeprefix("apool-"))])
        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        run = await run_service.create_run(db, ws, configuration_version_id=cv.id)
        run = await run_service.transition_run(db, run, "queued")
        await db.commit()
        return ws_id, cv.id, run.id


WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VARSET_ENDPOINT = "/api/v2/organizations/default/varsets"


async def _ws(client, name, labels=None, **attrs):
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {"name": name, "labels": labels or {}, **attrs},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


class TestGlobalVersusExplicitPrecedence:
    """A global set must NEVER beat an explicitly-assigned one on the same key.

    v1.5.4 returned globals first and explicit second, and resolution is
    last-write-wins, so explicit won. Reordering the resolver inverted that —
    silently, on `helm upgrade`, for workspaces nobody touched. No snapshot can
    see a change in list ORDER, which is why this needs a behavioural test.
    """

    async def test_explicit_assignment_beats_a_global_set(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"prec-{tag}")

        async with get_db_session() as db:
            glob = VariableSet(name=f"global-{tag}", global_set=True)
            expl = VariableSet(name=f"explicit-{tag}")
            db.add_all([glob, expl])
            await db.flush()
            db.add(
                VariableSetWorkspace(
                    variable_set_id=expl.id,
                    workspace_id=uuid.UUID(ws_id.removeprefix("ws-")),
                )
            )
            await db.commit()
            glob_id, expl_id = glob.id, expl.id

        for vs_id, value in ((glob_id, "from-global"), (expl_id, "from-explicit")):
            resp = await client.post(
                f"/api/v2/varsets/varset-{vs_id}/relationships/vars",
                json={
                    "data": {
                        "type": "vars",
                        "attributes": {"key": "WHO_WINS", "value": value, "category": "env"},
                    }
                },
                headers=AUTH,
            )
            assert resp.status_code == 201, resp.text

        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        winner = {v.key: v.value for v in resolved}["WHO_WINS"]
        assert winner == "from-explicit", (
            "a global set overrode an explicitly-assigned one — this silently "
            "changes what an untouched workspace injects, on upgrade"
        )


class TestAMalformedRuleCannotBreakTheEstate:
    """A rule that parses but selects nothing must not raise out of resolution.

    `applicable_varsets` walks every rule-bearing set for every workspace, so an
    exception escaping there is not one broken workspace — it is every run in
    the deployment failing to dispatch.
    """

    async def test_an_empty_dimension_rule_is_refused_at_write_time(self, app, client):
        set_auth(app, admin_user())
        for rule in ({"labels": {}}, {"workspace_ids": []}, {}):
            resp = await client.post(
                VARSET_ENDPOINT,
                json={
                    "data": {
                        "type": "varsets",
                        "attributes": {
                            "name": f"empty-{uuid.uuid4().hex[:8]}",
                            "assignment-rule": rule,
                        },
                    }
                },
                headers=AUTH,
            )
            assert resp.status_code in (201, 422), resp.text
            if resp.status_code == 201:
                assert resp.json()["data"]["attributes"]["assignment-rule"] is None, (
                    f"{rule} was stored as a rule; it selects nothing and will raise "
                    "inside resolution for every workspace in the estate"
                )

    async def test_a_stored_unusable_rule_never_breaks_resolution(self, app, client):
        """Belt and braces: even if such a rule reaches the database by another
        route — a restore, an older client, a direct edit — resolution must
        survive it."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"survive-{tag}")

        async with get_db_session() as db:
            db.add(VariableSet(name=f"broken-{tag}", assignment_rule={"labels": {}}))
            await db.commit()

        async with get_db_session() as db:
            # Must not raise. Before the fix this propagated a
            # WorkspaceFilterError out of next_run as a 500.
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        assert isinstance(resolved, list)


class TestAnEmptyDimensionCannotMatchEveryWorkspace:
    """`{"name_prefix": ""}` built a query with no WHERE clause.

    A blank field or a typo for "prod-" therefore handed a credential set to the
    whole estate — the exact outcome the `all` guard exists to prevent.
    """

    # owner_email is deliberately NOT here: its builder clause is gated on
    # `is not None`, so a blank value is a real selector (unowned workspaces),
    # not a missing one. Only the truthiness-gated dimensions can produce a
    # WHERE-less query.
    @pytest.mark.parametrize("rule", [{"name_prefix": ""}, {"name_glob": ""}])
    async def test_a_blank_dimension_is_refused(self, app, client, rule):
        set_auth(app, admin_user())
        resp = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"blank-{uuid.uuid4().hex[:8]}",
                        "assignment-rule": rule,
                    },
                }
            },
            headers=AUTH,
        )
        if resp.status_code == 201:
            assert resp.json()["data"]["attributes"]["assignment-rule"] is None, (
                f"{rule} stored as a rule — it matches EVERY workspace"
            )
        else:
            assert resp.status_code == 422, resp.text

    async def test_the_hyphenated_workspace_ids_spelling_is_also_refused(self, app, client):
        """parse_filter normalises hyphens, so the underscore-only guard was
        decorative."""
        set_auth(app, admin_user())
        resp = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"hyph-{uuid.uuid4().hex[:8]}",
                        "assignment-rule": {"workspace-ids": [f"ws-{uuid.uuid4()}"]},
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text


class TestAValueSourceFlipCannotDiscloseASecret:
    """Flipping value-source to vault without a reference left the stored
    plaintext in place, and the reference-is-not-a-secret display rule then
    returned it. VAR_WRITE has never implied "may read back secrets"."""

    async def test_patching_to_vault_without_a_reference_is_refused(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        # agent mode deliberately: on a local workspace the local-execution
        # guard raises the same 422 and this test would pass without the
        # missing-reference guard it exists to pin.
        ws_id = await _ws(client, f"disclose-{tag}", **{"execution-mode": "agent"})

        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "SECRET",
                        "value": "SUPER-SECRET-PLAINTEXT",
                        "category": "env",
                        "sensitive": True,
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["value"] is None, "masking is broken"
        var_id = resp.json()["data"]["id"]

        flip = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"value-source": "vault"}}},
            headers=AUTH,
        )
        assert flip.status_code == 422, (
            "flipping to vault without a reference was accepted; the stored "
            f"secret is now returned unmasked: {flip.text[:200]}"
        )

        after = await client.get(f"/api/v2/workspaces/{ws_id}/vars", headers=AUTH)
        values = [v["attributes"]["value"] for v in after.json()["data"]]
        assert "SUPER-SECRET-PLAINTEXT" not in values, "the secret leaked"

    async def test_patching_to_vault_with_a_reference_is_allowed(self, app, client):
        """The refusal above must not block the legitimate conversion."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"convert-{tag}", **{"execution-mode": "agent"})

        created = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"key": "TOKEN", "value": "old", "category": "env"},
                }
            },
            headers=AUTH,
        )
        var_id = created.json()["data"]["id"]

        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        flip = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"value-source": "vault", "value": ref}}},
            headers=AUTH,
        )
        assert flip.status_code == 200, flip.text
        assert flip.json()["data"]["attributes"]["value-source"] == "vault"


class TestVaultOnALocalWorkspace:
    """Vault resolution happens only on the listener claim path, so a
    local-execution workspace would silently receive nothing — no value, no
    error, no failed run. Refuse it rather than fail quietly."""

    async def test_a_vault_variable_is_refused_on_a_local_workspace(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"local-{tag}", **{"execution-mode": "local"})

        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        resp = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOKEN",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, (
            "accepted on a local workspace, where it silently resolves to nothing"
        )
        assert "local" in resp.json()["detail"].lower()


class TestVaultBackedVariableSetVariables:
    """The headline pairing: one Vault credential in a set, targeted by rule.

    The column, the resolver and docs/vault.md all promised this; no write path
    set it, so it could never be built — the operator had to create the same
    reference on every workspace, which is what variable sets exist to avoid.
    """

    async def test_a_varset_variable_can_carry_a_vault_reference(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        vs = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": f"vaultset-{tag}"}}},
            headers=AUTH,
        )
        vs_id = vs.json()["data"]["id"]

        ref = json.dumps({"mount": "secret", "path": "apps/shared", "field": "token"})
        created = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "SHARED_TOKEN",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        attrs = created.json()["data"]["attributes"]
        assert attrs["value-source"] == "vault"
        assert attrs["sensitive"] is True, "a vault reference resolves to a secret"
        # The reference is a path, not a secret — shown, like the workspace side.
        assert attrs["value"] == ref

    async def test_a_malformed_varset_reference_is_refused(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        vs = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": f"badset-{tag}"}}},
            headers=AUTH,
        )
        resp = await client.post(
            f"/api/v2/varsets/{vs.json()['data']['id']}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "X",
                        "category": "env",
                        "value-source": "vault",
                        "value": json.dumps({"mount": "secret"}),
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text

    async def test_it_reaches_resolution_carrying_its_source(self, app, client):
        """The point of wiring the write path: the resolver already honoured
        value_source, so this now completes the chain to the run."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(
            client, f"vsvault-{tag}", labels={"vsv": tag}, **{"execution-mode": "agent"}
        )
        vs = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"ruled-{tag}",
                        "assignment-rule": {"labels": {"vsv": tag}},
                    },
                }
            },
            headers=AUTH,
        )
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        await client.post(
            f"/api/v2/varsets/{vs.json()['data']['id']}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "RULED_TOKEN",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )

        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        by_key = {v.key: v for v in resolved}
        assert "RULED_TOKEN" in by_key, "rule-matched varset variable never reached the run"
        assert by_key["RULED_TOKEN"].value_source == "vault"


class TestTheFixesDidNotBreakOrdinaryEditing:
    """The missing-reference guard must fire on a *transition* only.

    Requiring a reference on every write broke every partial update of an
    existing vault variable — a description or key edit 422'd. Fixing a
    disclosure by making the feature unusable is not a fix.
    """

    async def _vault_var(self, client, tag):
        ws_id = await _ws(client, f"edit-{tag}", **{"execution-mode": "agent"})
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        created = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOK",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        return ws_id, created.json()["data"]["id"], ref

    @pytest.mark.parametrize(
        "patch_attrs",
        [
            {"description": "a note"},
            {"key": "RENAMED"},
            {"hcl": True},
        ],
    )
    async def test_a_partial_edit_of_a_vault_variable_still_works(self, app, client, patch_attrs):
        set_auth(app, admin_user())
        ws_id, var_id, ref = await self._vault_var(client, uuid.uuid4().hex[:8])
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": patch_attrs}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        # And the reference survived untouched.
        assert resp.json()["data"]["attributes"]["value"] == ref

    async def test_replacing_the_reference_is_still_validated(self, app, client):
        """The relaxation must not let junk replace a good reference."""
        set_auth(app, admin_user())
        ws_id, var_id, _ = await self._vault_var(client, uuid.uuid4().hex[:8])
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"value": "not-a-reference"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text


class TestVaultIsRefusedOnGitAuthCategories:
    """A git credential is a JSON envelope; a Vault field resolves to a string.

    resolve_git_auth json.loads a git-auth value and *skips* it when that fails,
    so a vault-sourced one was dropped and the run failed to fetch its private
    modules with nothing pointing at why. git_ssh_auth was worse — the value is
    passed through verbatim, handing the runner a bare secret where it expects
    {private_key, known_hosts, rewrite}. The workspace UI refused the pair; the
    API did not, so the varset form and every SDK/provider caller could make it.
    """

    REF = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})

    async def _workspace(self, client, tag):
        ws = await client.post(
            WS_ENDPOINT,
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {"name": f"gitvault-{tag}", "execution-mode": "agent"},
                }
            },
            headers=AUTH,
        )
        return ws.json()["data"]["id"]

    async def _varset(self, client, tag):
        vs = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": f"gitvault-{tag}"}}},
            headers=AUTH,
        )
        return vs.json()["data"]["id"]

    @pytest.mark.parametrize("category", ["git_http_auth", "git_ssh_auth"])
    async def test_creating_a_vault_git_auth_workspace_variable_is_refused(
        self, app, client, category
    ):
        set_auth(app, admin_user())
        ws_id = await self._workspace(client, uuid.uuid4().hex[:8])

        res = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "GIT_CRED",
                        "category": category,
                        "value-source": "vault",
                        "value": self.REF,
                    },
                }
            },
            headers=AUTH,
        )
        assert res.status_code == 422, res.text
        assert "vault" in res.text.lower()

    @pytest.mark.parametrize("category", ["git_http_auth", "git_ssh_auth"])
    async def test_creating_a_vault_git_auth_varset_variable_is_refused(
        self, app, client, category
    ):
        set_auth(app, admin_user())
        vs_id = await self._varset(client, uuid.uuid4().hex[:8])

        res = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "GIT_CRED",
                        "category": category,
                        "value-source": "vault",
                        "value": self.REF,
                    },
                }
            },
            headers=AUTH,
        )
        assert res.status_code == 422, res.text
        assert "vault" in res.text.lower()

    async def test_patching_a_static_git_auth_variable_to_vault_is_refused(self, app, client):
        """The category is already stored, so the guard must read it from the row
        rather than only from the incoming attributes."""
        set_auth(app, admin_user())
        vs_id = await self._varset(client, uuid.uuid4().hex[:8])

        created = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "GIT_CRED",
                        "category": "git_http_auth",
                        "value": json.dumps({"source": "static", "username": "u", "token": "t"}),
                    },
                }
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        var_id = created.json()["data"]["id"]

        res = await client.patch(
            f"/api/v2/varsets/{vs_id}/relationships/vars/{var_id}",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"value-source": "vault", "value": self.REF},
                }
            },
            headers=AUTH,
        )
        assert res.status_code == 422, res.text

    async def test_an_ordinary_env_variable_still_accepts_vault(self, app, client):
        """The negative path's negative: the guard must not over-reach."""
        set_auth(app, admin_user())
        vs_id = await self._varset(client, uuid.uuid4().hex[:8])

        res = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "SHARED_TOKEN",
                        "category": "env",
                        "value-source": "vault",
                        "value": self.REF,
                    },
                }
            },
            headers=AUTH,
        )
        assert res.status_code == 201, res.text


class TestVarsetVaultInvariants:
    """The varset write path must match the workspace one; it was a near-copy
    that dropped the vault clause."""

    async def _varset(self, client, tag):
        vs = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": f"vsinv-{tag}"}}},
            headers=AUTH,
        )
        return vs.json()["data"]["id"]

    async def test_sensitive_cannot_be_downgraded_on_a_vault_varset_variable(self, app, client):
        set_auth(app, admin_user())
        vs_id = await self._varset(client, uuid.uuid4().hex[:8])
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        created = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "K",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        var_id = created.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/varsets/{vs_id}/relationships/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"sensitive": False}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["sensitive"] is True, (
            "a vault reference resolves to a secret; sensitive must not be downgradable"
        )

    async def test_a_junk_value_cannot_replace_a_varset_reference(self, app, client):
        set_auth(app, admin_user())
        vs_id = await self._varset(client, uuid.uuid4().hex[:8])
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        created = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "K",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        resp = await client.patch(
            f"/api/v2/varsets/{vs_id}/relationships/vars/{created.json()['data']['id']}",
            json={"data": {"type": "vars", "attributes": {"value": "not-json"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text


class TestRulePrecedenceIsPinned:
    """The fix introduced a total order: global < rule < explicit.

    Only global-vs-explicit was covered. The other two comparisons are new
    behaviour, so they are pinned here — reordering `rank` would otherwise
    silently change which credential a rule-matched workspace receives.
    """

    async def _set_with_var(self, client, name, value, **attrs):
        vs = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": name, **attrs}}},
            headers=AUTH,
        )
        vs_id = vs.json()["data"]["id"]
        await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"key": "WHO_WINS", "value": value, "category": "env"},
                }
            },
            headers=AUTH,
        )
        return vs_id

    async def _winner(self, ws_id):
        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        return {v.key: v.value for v in resolved}.get("WHO_WINS")

    async def test_a_rule_matched_set_beats_a_global_one(self, app, client):
        """A rule is a deliberate targeting decision; global is the fallback."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"rvg-{tag}", labels={"rvg": tag})
        await self._set_with_var(client, f"g-{tag}", "from-global", **{"global": True})
        await self._set_with_var(
            client, f"r-{tag}", "from-rule", **{"assignment-rule": {"labels": {"rvg": tag}}}
        )
        assert await self._winner(ws_id) == "from-rule"

    async def test_an_explicit_assignment_beats_a_rule_matched_set(self, app, client):
        """Explicit is the most specific statement of intent there is."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"rve-{tag}", labels={"rve": tag})
        await self._set_with_var(
            client, f"r-{tag}", "from-rule", **{"assignment-rule": {"labels": {"rve": tag}}}
        )
        expl = await self._set_with_var(client, f"e-{tag}", "from-explicit")
        await client.post(
            f"/api/v2/varsets/{expl}/relationships/workspaces",
            json={"data": [{"id": ws_id, "type": "workspaces"}]},
            headers=AUTH,
        )
        assert await self._winner(ws_id) == "from-explicit"


class TestTheBlastRadiusViewSurvivesABadRule:
    """`_rule_matches` was guarded and its sibling was not.

    The association view is the screen that answers "who currently receives
    this credential" — the one thing that must not 500 on the bad rule you are
    trying to find.
    """

    async def test_the_varset_view_does_not_500_on_an_unusable_rule(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        async with get_db_session() as db:
            # Stored directly: such a rule is refused at write time now, but one
            # saved on an earlier build is exactly the case this must survive.
            vs = VariableSet(name=f"badview-{tag}", assignment_rule={"labels": {}})
            db.add(vs)
            await db.commit()
            vs_id = vs.id

        resp = await client.get(
            f"/api/terrapod/v1/varsets/varset-{vs_id}/relationships/workspaces", headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == [], "an unusable rule must match nothing, not raise"


class TestTheSelectorNarrowingIsScoped:
    """Excluding blank values wholesale was a breaking change.

    `owner_email` is NOT NULL defaulting to "", so `{"owner_email": ""}` is the
    only way to select unowned workspaces and worked before — its builder
    clause is gated on `is not None`, not truthiness.
    """

    async def test_a_blank_owner_email_still_selects(self, app, client):
        set_auth(app, admin_user())
        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/search",
            json={"filter": {"owner_email": ""}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("blank", [{"name_prefix": ""}, {"name_glob": ""}])
    async def test_a_blank_truthiness_gated_dimension_is_still_refused(self, app, client, blank):
        set_auth(app, admin_user())
        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/search", json={"filter": blank}, headers=AUTH
        )
        assert resp.status_code == 422, (
            f"{blank} builds no WHERE clause — on bulk-update it would mutate the estate"
        )


class TestATransientVaultFailureDoesNotDestroyTheRun:
    """A 30-second Vault restart and a malformed reference are not the same
    thing, and were being treated identically.

    Erroring the run on a connection-class failure means an operator re-queues
    by hand after every Vault blip — and because the listener stops its drain
    pass on the 204, one broken run took the rest of the batch with it.
    """

    async def test_an_unreachable_vault_leaves_the_run_queued(self, app, client):
        from terrapod.config import VaultInstanceConfig, settings
        from terrapod.services.vault_client import VaultUnavailable

        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_id, listener_id = await _pool_with_listener(client, tag)
        ws_id, cv_id, run_id = await _agent_run_with_vault_var(client, tag, pool_id)
        set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))

        prior = (settings.vault.enabled, settings.vault.instances)
        settings.vault.enabled = True
        settings.vault.instances = [
            VaultInstanceConfig(name="default", default=True, address="https://vault.test:8200")
        ]
        try:
            with patch(
                "terrapod.services.vault_source_service.read_secret",
                new=AsyncMock(side_effect=VaultUnavailable("connection refused")),
            ):
                resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        finally:
            settings.vault.enabled, settings.vault.instances = prior

        assert resp.status_code == 204, resp.text
        async with get_db_session() as db:
            final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert final.status == "queued", (
            f"a transient Vault failure destroyed the run (status={final.status}); "
            "it should wait for the next claim"
        )

    async def test_a_permanent_failure_still_errors_the_run(self, app, client):
        """The relaxation must not swallow a reference that will never resolve."""
        from terrapod.config import VaultInstanceConfig, settings
        from terrapod.services.vault_client import VaultError

        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_id, listener_id = await _pool_with_listener(client, tag)
        ws_id, cv_id, run_id = await _agent_run_with_vault_var(client, tag, pool_id)
        set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))

        prior = (settings.vault.enabled, settings.vault.instances)
        settings.vault.enabled = True
        settings.vault.instances = [
            VaultInstanceConfig(name="default", default=True, address="https://vault.test:8200")
        ]
        try:
            with patch(
                "terrapod.services.vault_source_service.read_secret",
                new=AsyncMock(side_effect=VaultError("Vault denied 'secret/apps/x'")),
            ):
                resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        finally:
            settings.vault.enabled, settings.vault.instances = prior

        assert resp.status_code == 204
        async with get_db_session() as db:
            final = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert final.status == "errored"


class TestFlippingAwayFromVault:
    """A vault→static flip left the JSON reference in place as the literal
    value, and the runner then delivered `{"mount":...}` to terraform as the
    credential. Silent, and symmetric with the flip the other way."""

    async def test_flipping_to_static_requires_a_replacement_value(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"unflip-{tag}", **{"execution-mode": "agent"})
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        created = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOK",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        var_id = created.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"value-source": "static"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, (
            "the reference would have been delivered to terraform as the value"
        )

    async def test_flipping_to_static_with_a_value_works(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"unflip2-{tag}", **{"execution-mode": "agent"})
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        created = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOK",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}/vars/{created.json()['data']['id']}",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"value-source": "static", "value": "a-real-value"},
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["value-source"] == "static"


class TestSwitchingAWorkspaceToLocalExecution:
    """The create-time refusal was bypassable by creating on agent and then
    switching the workspace to local."""

    async def test_it_is_refused_while_vault_variables_exist(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"switch-{tag}", **{"execution-mode": "agent"})
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOK",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )

        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"type": "workspaces", "attributes": {"execution-mode": "local"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, "switching to local silently strands the vault variable"
        assert "vault" in resp.json()["detail"].lower()


class TestNarrowedLabelReservations:
    """Seven reservations were released in v1.6.0 (#1450).

    They were held on the reasoning that reserving costs nothing — but only
    `status` was ever implemented as a virtual filter term, so the rest refused
    the label *and* gave the filter nothing in return. These are the words
    people reach for when labelling a workspace.
    """

    @pytest.mark.parametrize(
        "key",
        ["pool", "mode", "backend", "drift", "version", "vcs", "locked", "branch"],
    )
    async def test_a_released_key_is_usable_as_a_label(self, app, client, key):
        set_auth(app, admin_user())
        resp = await client.post(
            WS_ENDPOINT,
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {
                        "name": f"rel-{key}-{uuid.uuid4().hex[:8]}",
                        "labels": {key: "x"},
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["attributes"]["labels"][key] == "x"

    @pytest.mark.parametrize("key", ["status", "owner"])
    async def test_the_remaining_reservations_still_hold(self, app, client, key):
        """Both survivors mislead about a real decision if used as a label:
        `status` is the sole built-in filter term (`status:errored` would be
        ambiguous), and `owner` maps to `owner_email`, which grants admin."""
        set_auth(app, admin_user())
        resp = await client.post(
            WS_ENDPOINT,
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {
                        "name": f"res-{key}-{uuid.uuid4().hex[:8]}",
                        "labels": {key: "x"},
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text

    async def test_a_released_key_works_in_an_assignment_rule(self, app, client):
        """The point of releasing them: they become usable for the label-based
        targeting that is Terrapod's answer to grouping."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"relrule-{tag}", labels={"version": tag})
        vs = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"relvs-{tag}",
                        "assignment-rule": {"labels": {"version": tag}},
                    },
                }
            },
            headers=AUTH,
        )
        assert vs.status_code == 201, vs.text
        resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/varsets", headers=AUTH)
        assert vs.json()["data"]["id"] in {v["id"] for v in resp.json()["data"]}


class TestTheVaultValueActuallyReachesTheRunner:
    """The whole point of #1439, and it had NO test.

    Every vault test in the release mocked `read_secret` with a raised
    exception, so the failure paths were well covered and the success path was
    not covered at all. Deleting the substitution loop in `runs.py` left the
    entire suite green while the runner was handed the JSON reference
    `{"mount":...,"path":...,"field":...}` as the credential — the identical
    failure that `TestFlippingAwayFromVault` exists to catch on the write path.
    """

    async def test_a_resolved_secret_replaces_the_reference_in_env_vars(self, app, client):
        from terrapod.config import VaultInstanceConfig, settings

        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        pool_id, listener_id = await _pool_with_listener(client, tag)
        ws_id, cv_id, run_id = await _agent_run_with_vault_var(client, tag, pool_id)
        set_listener_auth(app, listener_id, pool_id.removeprefix("apool-"))

        prior = (settings.vault.enabled, settings.vault.instances)
        settings.vault.enabled = True
        settings.vault.instances = [
            VaultInstanceConfig(name="default", default=True, address="https://vault.test:8200")
        ]
        try:
            with patch(
                "terrapod.services.vault_source_service.read_secret",
                new=AsyncMock(return_value="s3cr3t-from-vault"),
            ):
                resp = await client.get(f"/api/terrapod/v1/listeners/{listener_id}/runs/next")
        finally:
            settings.vault.enabled, settings.vault.instances = prior

        assert resp.status_code == 200, resp.text
        env = {e["key"]: e["value"] for e in resp.json()["data"]["attributes"]["env-vars"]}
        assert env.get("TOK") == "s3cr3t-from-vault", env

        # And the reference itself must be gone — handing the runner the JSON
        # coordinates as the credential is the failure mode, not a lesser one.
        body = resp.text
        assert '"mount"' not in body, "the vault reference was delivered instead of the secret"
        assert "apps/x" not in body


class TestBulkSwitchToLocalHonoursTheVaultGuard:
    """B1: the single-workspace PATCH refuses switching an agent workspace with
    Vault-sourced variables to local (they resolve only on the agent claim path
    and would silently deliver nothing). The bulk-update path was the way round
    that guard — a third bypass after the two the first pass closed."""

    async def test_bulk_switch_to_local_is_refused_when_a_vault_var_is_present(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(
            client, f"blkvault-{tag}", labels={"grp": tag}, **{"execution-mode": "agent"}
        )
        ref = json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})
        made = await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "TOK",
                        "category": "env",
                        "value-source": "vault",
                        "value": ref,
                    },
                }
            },
            headers=AUTH,
        )
        assert made.status_code == 201, made.text

        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/bulk-update",
            json={
                "dry_run": False,
                "filter": {"labels": {"grp": tag}},
                "update": {"execution-mode": "local"},
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text
        assert f"blkvault-{tag}" in resp.json()["detail"]

        # And the workspace was NOT switched (all-or-nothing).
        got = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        assert got.json()["data"]["attributes"]["execution-mode"] == "agent"

    async def test_bulk_switch_to_local_is_fine_without_vault_vars(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"blkplain-{tag}", labels={"grp": tag}, **{"execution-mode": "agent"})
        resp = await client.post(
            "/api/terrapod/v1/workspaces/actions/bulk-update",
            json={
                "dry_run": False,
                "filter": {"labels": {"grp": tag}},
                "update": {"execution-mode": "local"},
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
