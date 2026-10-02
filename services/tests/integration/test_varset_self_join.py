"""A workspace cannot be shaped to pull in a variable set (GHSA-49q6-pm68-3xgw).

Integration tier, driving the real routes, because that is what the finding is: the
reporter created a workspace through the API as a non-admin and watched another
team's rule-assigned secret set become applicable to it. The guard's mechanism is
"flush the pending row, then ask the real SQL selector about it", so a mocked
session could not exercise it and a fake answering from a fixture would be testing
the fixture.
"""

import uuid

import pytest

from tests.integration.conftest import AUTH, admin_user, regular_user, set_auth

pytestmark = pytest.mark.integration

WS = "/api/v2/organizations/default/workspaces"
VARSETS = "/api/v2/organizations/default/varsets"


async def _varset(client, name, *, rule=None, global_set=False, secret=True):
    """Create a variable set. `secret=True` adds a sensitive variable.

    That matters: the refusal is scoped to sets that actually hold something worth
    taking, so a set of plain configuration is deliberately allowed to join. A
    fixture without a secret would make every refusal test silently vacuous.
    """
    attrs = {"name": name, "global": global_set}
    if rule is not None:
        attrs["assignment-rule"] = rule
    resp = await client.post(
        VARSETS, json={"data": {"type": "varsets", "attributes": attrs}}, headers=AUTH
    )
    assert resp.status_code in (200, 201), resp.text
    vs_id = resp.json()["data"]["id"]
    if not secret:
        # A set with NO variables holds no secrets trivially, which makes the
        # "plain configuration may still join" test pass however the secret check is
        # written — including when it is a no-op. Give it a real, ordinary variable
        # so the test distinguishes "no secrets" from "nothing at all".
        r = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "log_level",
                        "value": "debug",
                        "category": "terraform",
                        "sensitive": False,
                    },
                }
            },
            headers=AUTH,
        )
        assert r.status_code in (200, 201), r.text
    if secret:
        r = await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "db_password",
                        "value": "TEAM-A-SECRET",
                        "category": "terraform",
                        "sensitive": True,
                    },
                }
            },
            headers=AUTH,
        )
        assert r.status_code in (200, 201), r.text
    return vs_id


async def _create_ws(client, name, **attrs):
    return await client.post(
        WS,
        json={"data": {"type": "workspaces", "attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )


class TestTheReportedEscalation:
    async def test_a_non_admin_cannot_create_a_workspace_that_matches_the_rule(self, app, client):
        """The reporter's proof of concept, as a test: admin makes a rule-scoped
        set, a non-admin creates a workspace carrying the matching label."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"probe-{tag}", labels={"team": tag})

        assert resp.status_code == 403, resp.text
        # Name the set, so the operator knows what to ask about.
        assert f"teamA-{tag}" in resp.text
        assert "GHSA-49q6" in resp.text

    async def test_and_the_workspace_is_not_left_behind(self, app, client):
        """The refusal happens after a flush, so a rollback that did not fire would
        leave a committed workspace behind and the next create would 409 on the
        name — a refusal that still half-succeeded."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        assert (await _create_ws(client, f"probe-{tag}", labels={"team": tag})).status_code == 403

        # the name is free, which is only true if nothing was committed
        set_auth(app, admin_user())
        resp = await _create_ws(client, f"probe-{tag}", labels={"other": "x"})
        assert resp.status_code == 201, resp.text

    async def test_a_non_admin_cannot_patch_a_workspace_into_the_rule(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        user = regular_user(f"probe-{tag}@test.com")
        set_auth(app, user)
        resp = await _create_ws(client, f"probe-{tag}", labels={})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {"team": tag}}}},
            headers=AUTH,
        )
        assert resp.status_code == 403, resp.text
        assert f"teamA-{tag}" in resp.text

    async def test_a_platform_admin_may_do_both(self, app, client):
        """An admin already reads every variable set, so there is nothing to
        escalate to — and refusing them would make the feature unusable, since an
        admin is the only principal who can set it up at all."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        resp = await _create_ws(client, f"adminws-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text

        resp2 = await _create_ws(client, f"adminws2-{tag}", labels={})
        ws_id = resp2.json()["data"]["id"]
        resp3 = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {"team": tag}}}},
            headers=AUTH,
        )
        assert resp3.status_code == 200, resp3.text


class TestWhatMustSTILLBeAllowed:
    """A guard that refuses ordinary edits gets switched off, so these carry as
    much weight as the refusals."""

    async def test_an_ordinary_create_with_no_matching_rule(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": "something-else"})
        assert resp.status_code == 201, resp.text

    async def test_an_unrelated_patch_on_a_workspace_that_already_matches(self, app, client):
        """The comparison is growth, not presence. A workspace an admin legitimately
        put in the set must stay editable by whoever administers it."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})
        resp = await _create_ws(client, f"blessed-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        # a non-admin with admin ON the workspace via the everyone/owner path is
        # not modelled here; the point is that the GUARD does not object, so drive
        # it as the admin-created owner would be.
        set_auth(app, admin_user())
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"description": "unrelated"}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

    async def test_dropping_a_label_that_came_to_pull_a_set_in(self, app, client):
        """Shrinking is a de-escalation and must not need an admin.

        The realistic way a non-admin's own workspace comes to match is ORDER: they
        create it with a label, and an admin later writes a rule that happens to
        select it. Refusing the owner's attempt to drop that label would leave them
        unable to get out of a set they never opted into — the guard holding the
        workspace in the very state it exists to prevent.
        """
        tag = uuid.uuid4().hex[:8]
        user = regular_user(f"probe-{tag}@test.com")

        # 1. the workspace exists first, with the label, and nothing matches it
        set_auth(app, user)
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        # 2. an admin then writes a rule that selects it
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        # 3. the owner drops the label: strictly fewer sets reach the workspace
        set_auth(app, user)
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {}}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

    async def test_a_global_set_is_not_growth(self, app, client):
        """A global set already reaches every workspace, so counting it would refuse
        the first workspace anyone creates."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"everywhere-{tag}", global_set=True)

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}")
        assert resp.status_code == 201, resp.text


class TestTheGuardIsWiredIntoBothPaths:
    """The service is inert unless the routers call it, and create and PATCH are
    separate call sites. The reporter's proof of concept used create, so gating
    only PATCH would leave the demonstrated attack working."""

    def test_create_and_patch_both_consult_it(self):
        """A presence check, and it says so — the enforcement itself is covered by the
        route-driven tests above.

        `src.count("refuse_varset_growth(") >= 2` reads like it proves both call sites
        are live and does not: wrapping either in `if False:` leaves the count at 2
        while the guard is dead. Verified, for both sites. What catches that is
        behavioural — four of this file's tests fail when the create guard is disabled
        and one when the PATCH guard is, which is the coverage that matters.

        This is kept for the thing those cannot do: fail when someone removes a call
        site outright, or adds a third workspace-write path without one.
        """
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert src.count("refuse_varset_growth(") >= 2, (
            "a workspace-write path no longer reaches the guard at all"
        )
        assert "before=set()" in src, "create does not treat a new workspace as starting empty"
        assert "before=_varsets_before_patch" in src, (
            "PATCH does not compare against a pre-change snapshot"
        )

    def test_the_snapshot_is_taken_before_any_attribute_is_applied(self):
        """Taken after an attribute moved, the comparison straddles nothing and the
        guard permits exactly the edit it exists to refuse."""
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2.update_workspace)
        snap = src.index("_varsets_before_patch = ")
        writes = [src.index(m) for m in ("ws.name = ", "ws.description = ") if m in src]
        assert writes, "no attribute writes found — this test is stale"
        assert snap < min(writes), (
            "the snapshot is taken after an attribute has already been applied"
        )


class TestTheSkipOptimisationCannotSilentlyGoStale:
    """The PATCH path skips its three queries when the body cannot move the answer.

    The first version of that was an allowlist of *triggering* keys, so it failed
    closed-to-skip: an attribute nobody had classified escaped the check entirely,
    while the docstring claimed the opposite and the test named `fails_open`
    asserted the opposite in its own body. Both reviewers found it. It is now a
    denylist, and these pin the direction.
    """

    def test_an_unclassified_attribute_still_pays_for_the_check(self):
        from terrapod.services.varset_self_join import touches_rule_selectable

        assert touches_rule_selectable({"something-nobody-has-classified": 1})
        assert touches_rule_selectable({"labels": {}})
        # and a body that provably cannot move it does not pay
        assert not touches_rule_selectable({"description": "x", "auto-apply": True})

    def test_a_relationship_always_counts(self):
        """The connection arrives as a relationship as well as an attribute, and
        gating on the attribute alone is how the PATCH gate was got wrong once."""
        from terrapod.services.varset_self_join import touches_rule_selectable

        assert touches_rule_selectable({}, {"vcs-connection": {"data": None}})
        assert touches_rule_selectable({}, {"anything-at-all": {}})

    def test_nothing_in_the_denylist_is_a_filter_dimension(self):
        """The denylist is the only thing that can switch the guard off, so an entry
        that names something a rule CAN select on would be a silent hole. Checked
        against `WorkspaceFilter` itself rather than trusted."""
        from terrapod.services.varset_self_join import NOT_RULE_SELECTABLE
        from terrapod.services.workspace_search_service import WorkspaceFilter

        # attribute key -> filter field, for the dimensions a PATCH can move
        selectable = {
            "labels": "labels",
            "name": "name_prefix",
            "execution-backend": "execution_backend",
            "execution-mode": "execution_mode",
            # One dimension, two wire spellings. `WorkspaceFilter` names the
            # column (`engine_version`, #1559) while the router still accepts
            # `terraform-version` and normalises it — so BOTH keys have to be
            # checked. Mapping only the new one would leave a PATCH using the
            # legacy spelling able to move the dimension without paying the
            # self-join guard, which is exactly the hole this test exists to
            # close. Confirmed against the router rather than assumed:
            # `tfe_v2.py` reads `if "engine-version" in attrs or
            # "terraform-version" in attrs`.
            "engine-version": "engine_version",
            "terraform-version": "engine_version",
            "agent-pool-id": "agent_pool_id",
            "vcs-connection-id": "vcs_connection_id",
            "owner-email": "owner_email",
        }
        for attr, field in selectable.items():
            assert field in WorkspaceFilter.model_fields, (
                f"this test maps {attr} to a filter field that no longer exists"
            )
            assert attr not in NOT_RULE_SELECTABLE, (
                f"{attr} moves the {field} dimension but is on the denylist, so a "
                "PATCH touching it would skip the self-join check"
            )

    def test_the_refused_dimensions_are_refused_at_both_ends(self):
        """`drift_status` and `locked` are platform state a workspace's own owner can
        move — through `dismiss-drift`, through disabling drift detection, and through
        lock/unlock — none of which pays the guard. They are refused as selectors
        instead of gating five more endpoints."""
        from terrapod.services.varset_self_join import RULE_DIMENSIONS_REFUSED
        from terrapod.services.workspace_search_service import WorkspaceFilter

        assert set(RULE_DIMENSIONS_REFUSED) == {"drift_status", "locked"}
        for dim in RULE_DIMENSIONS_REFUSED:
            assert dim in WorkspaceFilter.model_fields, (
                f"{dim} is refused but is no longer a filter dimension — the refusal "
                "is now dead code"
            )
            assert RULE_DIMENSIONS_REFUSED[dim].strip(), "every refusal needs its reason"

    async def test_a_rule_naming_a_refused_dimension_is_422(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        resp = await client.post(
            VARSETS,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"drifty-{tag}",
                        "global": False,
                        "assignment-rule": {"drift_status": "", "labels": {"team": tag}},
                    },
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text
        assert "drift_status" in resp.text

    async def test_a_stored_rule_naming_one_matches_nothing(self, db_session=None):
        """A deployment that already has such a rule must stop honouring it, not keep
        the hole open for exactly the installs that have one."""
        from terrapod.services.variable_service import _rule_matches

        assert not await _rule_matches(None, {"locked": True}, uuid.uuid4())
        assert not await _rule_matches(None, {"drift_status": ""}, uuid.uuid4())


class TestEveryWayAWorkspaceComesIntoExistence:
    """Three code paths construct a `Workspace` directly and so bypass the router
    guard. Each needs a decision, and two of the three are fine — but only because
    of who can reach them, which is a fact that can change.
    """

    def test_catalog_provision_is_gated(self):
        """It takes CALLER-SUPPLIED labels and needs only catalog `use` plus pool
        `write`, nowhere near platform admin. Without the check a non-admin could
        label a provisioned workspace into another team's rule-assigned set and
        receive its secrets in the run the provision queues — the reported
        escalation through a different door.
        """
        import inspect

        from terrapod.api.routers import catalog

        src = inspect.getsource(catalog)
        assert "refuse_varset_growth(" in src, (
            "catalog provision accepts user-supplied labels and does not consult "
            "the self-join guard, so it is an open path to the same escalation"
        )

    def test_the_two_admin_only_paths_are_recorded_as_such(self):
        """Autodiscovery rule creation and deleted-workspace restore both construct
        a Workspace directly and are NOT gated. That is correct because both are
        `require_admin` — an admin chose those labels — but it is correct only for
        that reason, so the reason is asserted rather than assumed. If either opens
        up, this fails and the guard has to follow.
        """
        import inspect

        from terrapod.api.routers import autodiscovery_rules, deleted_workspaces

        for mod, fn in (
            (autodiscovery_rules, "create_rule"),
            (deleted_workspaces, "restore_deleted_workspace"),
        ):
            src = inspect.getsource(getattr(mod, fn))
            assert "require_admin" in src, (
                f"{mod.__name__}.{fn} is no longer admin-only, so the labels it "
                "writes are no longer an admin's choice and it needs the "
                "self-join guard that create and catalog provision have"
            )


class TestOnlySetsHoldingSecretsAreRefused:
    """The refusal is scoped to the reported impact — "sensitive static values and
    Vault-brokered secrets" — because refusing every match would close the feature
    as well as the finding.

    The documented workflow is an admin writing "label `env=prod` -> varset" and
    developers self-servicing matching workspaces; the service catalog is entirely
    non-admin self-service. A blanket refusal breaks both, and a guard that refuses
    ordinary work gets switched off, taking the secrets with it.
    """

    async def test_a_set_of_plain_configuration_may_still_be_joined(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"plain-{tag}", rule={"labels": {"team": tag}}, secret=False)

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text

    async def test_a_set_with_a_sensitive_variable_is_refused(self, app, client):
        """The contrast is the test. Same shape, one sensitive variable."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"secret-{tag}", rule={"labels": {"team": tag}}, secret=True)

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": tag})
        assert resp.status_code == 403, resp.text

    async def test_a_brokered_value_is_refused(self, app, client):
        """The OpenBao/Vault case, where the secret never sits in the column.

        Measured rather than assumed: this router stores a vault-sourced variable
        with `sensitive` forced TRUE (`_apply_value_source` returns
        `force_sensitive`), so the `sensitive` clause alone already catches anything
        written through the API — removing the `value_source` clause does not fail
        this test, and that is correct rather than a gap in it.

        The clause stays as defence for rows this router did not write: a migration,
        a direct SQL fixup, or a future writer that sets `value_source` without
        setting `sensitive`. It is not load-bearing today and is not claimed to be.
        """
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        vs = await _varset(client, f"brokered-{tag}", rule={"labels": {"team": tag}}, secret=False)
        r = await client.post(
            f"/api/v2/varsets/{vs}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {
                        "key": "db_password",
                        "value": '{"mount":"secret","path":"apps/demo","field":"token"}',
                        "category": "terraform",
                        "sensitive": False,
                        "value-source": "vault",
                    },
                }
            },
            headers=AUTH,
        )
        # No skip: if the brokered shape stops being accepted, this test must FAIL
        # rather than quietly pass over the half of the check that matters most.
        assert r.status_code in (200, 201), r.text

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": tag})
        assert resp.status_code == 403, resp.text
