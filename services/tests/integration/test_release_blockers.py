"""Regression tests for the v1.6.0 release blockers.

Each of these fails on the code as first written. They are grouped here rather
than scattered because they share a cause worth naming: every one lives in a
write path, an upgrade path, a failure path, or an execution mode that the
original tests did not exercise. The features' happy paths were well covered
and none of these was visible to any contract snapshot.
"""

import json
import uuid

import pytest

from terrapod.db.models import VariableSet, VariableSetWorkspace
from terrapod.db.session import get_db_session
from terrapod.services import variable_service
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

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

    @pytest.mark.parametrize("rule", [{"name_prefix": ""}, {"name_glob": ""}, {"owner_email": ""}])
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
        ws_id = await _ws(client, f"disclose-{tag}")

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
