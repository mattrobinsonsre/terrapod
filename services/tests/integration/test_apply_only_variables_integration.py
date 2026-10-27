"""Apply-only variables, against the real database (#2189).

Integration tier for two reasons the services tier cannot reach.

The first is the uniqueness constraint. The whole design rests on a workspace
being able to hold `AWS_ROLE_ARN` twice -- once for a plan, once for an apply --
which required widening `uq_variables_workspace_key` to include `apply_only`.
Whether that widening permits the pair and still refuses a genuine duplicate is
a property of the constraint, not of application code, so only Postgres can
answer it. A mocked session would assert that an INSERT was issued.

The second is the precedence inversion. Resolution overlays an apply-only row on
its standard twin, and the dangerous way to build that is a second pass over the
whole chain, which would let a *workspace* apply-only row beat a *priority
variable set's* standard row -- inverting the authority order priority sets exist
to establish. The services tier pins it with mocks; here the sets, the
assignments and the rows are real, and `_get_applicable_varsets` runs its own SQL.

The ceiling case is deliberate: a key carrying ONLY an apply-only row must be
absent from a plan entirely, not present and empty. An empty value reaches
terraform as a declared variable, which is a different thing from an undeclared
one.
"""

import uuid

import pytest
from sqlalchemy import select, text

from terrapod.db.models import (
    Variable,
    VariableSet,
    VariableSetVariable,
    VariableSetWorkspace,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.services.variable_service import resolve_variables
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VARSET_ENDPOINT = "/api/v2/organizations/default/varsets"


def _var_body(key: str, value: str, *, apply_only: bool, configure_only: bool = False) -> dict:
    return {
        "data": {
            "type": "vars",
            "attributes": {
                "key": key,
                "value": value,
                "category": "env",
                "apply-only": apply_only,
                "configure-only": configure_only,
            },
        }
    }


def _by_key(resolved: list) -> dict[str, object]:
    """resolve_variables returns a LIST, so index it here rather than at each use.

    Phase-blind resolution deliberately keeps both rows for a key, so this
    helper is only valid where a phase was supplied.
    """
    out: dict[str, object] = {}
    for v in resolved:
        assert v.key not in out, f"{v.key} resolved twice; this helper assumes one row per key"
        out[v.key] = v
    return out


async def _seed_workspace(name: str) -> uuid.UUID:
    """A workspace created through the ORM, as the other integration tests do.

    The API returns a typed id (`ws-...`) which is not a bare UUID, and these
    tests need the real primary key to seed rows against. Going through the ORM
    is also the honest shape: what is under test is resolution, not creation.
    """
    async with get_db_session() as db:
        ws = Workspace(name=f"{name}-{uuid.uuid4().hex[:8]}")
        db.add(ws)
        await db.commit()
        return ws.id


async def _workspace(client, name: str) -> str:
    body = {"data": {"type": "workspaces", "attributes": {"name": name}}}
    resp = await client.post(WS_ENDPOINT, json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


class TestTheConstraintPermitsThePairAndNothingMore:
    """The widening is the feature; a silently disabled constraint is the hazard."""

    async def test_a_workspace_holds_one_key_twice_when_the_flags_differ(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"pair-{uuid.uuid4().hex[:8]}")

        plain = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("AWS_ROLE_ARN", "arn:plan", apply_only=False),
            headers=AUTH,
        )
        assert plain.status_code == 201, plain.text

        apply_only = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("AWS_ROLE_ARN", "arn:apply", apply_only=True),
            headers=AUTH,
        )
        assert apply_only.status_code == 201, (
            "the pair is the point of the feature; a 409 here means the constraint "
            "was never widened"
        )

        listing = await client.get(f"/api/v2/workspaces/{ws}/vars", headers=AUTH)
        rows = [v for v in listing.json()["data"] if v["attributes"]["key"] == "AWS_ROLE_ARN"]
        assert len(rows) == 2
        assert {v["attributes"]["apply-only"] for v in rows} == {False, True}

    @pytest.mark.parametrize("apply_only", [False, True])
    async def test_a_genuine_duplicate_is_still_refused(self, app, client, apply_only):
        set_auth(app, admin_user())
        # Both flag values, because a constraint that lost one column refuses
        # neither -- and Postgres treats NULLs as distinct, which is why the
        # column is NOT NULL rather than nullable.
        ws = await _workspace(client, f"dup-{uuid.uuid4().hex[:8]}")
        first = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("DUP", "one", apply_only=apply_only),
            headers=AUTH,
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("DUP", "two", apply_only=apply_only),
            headers=AUTH,
        )
        assert second.status_code == 409, (
            f"a second apply_only={apply_only} row for the same key must still "
            f"collide; got {second.status_code}"
        )

    async def test_a_variable_set_holds_one_key_twice_when_the_flags_differ(self, app, client):
        set_auth(app, admin_user())
        name = f"vs-{uuid.uuid4().hex[:8]}"
        created = await client.post(
            VARSET_ENDPOINT,
            json={"data": {"type": "varsets", "attributes": {"name": name}}},
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        vs = created.json()["data"]["id"]

        for value, flag in (("set:plan", False), ("set:apply", True)):
            resp = await client.post(
                f"/api/v2/varsets/{vs}/relationships/vars",
                json=_var_body("SHARED", value, apply_only=flag),
                headers=AUTH,
            )
            assert resp.status_code == 201, f"apply_only={flag}: {resp.text}"

        dup = await client.post(
            f"/api/v2/varsets/{vs}/relationships/vars",
            json=_var_body("SHARED", "again", apply_only=True),
            headers=AUTH,
        )
        assert dup.status_code == 409, "the set's own constraint must refuse a true duplicate"


class TestTheConflictAnEditCanCreate:
    """Clearing the flag can collide with a twin it coexisted with a moment ago.

    This is the failure mode the feature introduces, and the one an operator
    cannot diagnose unaided: two rows sit happily side by side, you clear the
    apply-only checkbox on one, and the pair becomes a duplicate. The global
    integrity net answers that with "Resource already exists or violates a
    constraint", which names neither the key nor the reason.

    So the message must name the key and say which slot is taken. These assert
    the message, not just the status, because the status alone is what left the
    operator stuck.
    """

    async def test_clearing_apply_only_into_a_collision_says_what_collided(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"clear-{uuid.uuid4().hex[:8]}")
        for flag in (False, True):
            resp = await client.post(
                f"/api/v2/workspaces/{ws}/vars",
                json=_var_body("ROLE", f"v-{flag}", apply_only=flag),
                headers=AUTH,
            )
            assert resp.status_code == 201, resp.text
            if flag:
                apply_only_id = resp.json()["data"]["id"]

        # The edit that creates the conflict.
        resp = await client.patch(
            f"/api/v2/workspaces/{ws}/vars/{apply_only_id}",
            json={"data": {"type": "vars", "attributes": {"apply-only": False}}},
            headers=AUTH,
        )
        assert resp.status_code == 409, f"expected a conflict, got {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        assert "ROLE" in detail, f"the message must name the key: {detail!r}"
        assert "a plan+apply variable" in detail, (
            f"the message must say which slot is taken -- the caller asked for "
            f"plan+apply and that is the one held: {detail!r}"
        )
        assert "apply-only" in detail, f"the message must state the rule: {detail!r}"
        assert "violates a constraint" not in detail, (
            "the generic integrity net must not be what answers this"
        )

    async def test_setting_apply_only_into_a_collision_names_that_slot_instead(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"set-{uuid.uuid4().hex[:8]}")
        ids = {}
        for flag in (False, True):
            resp = await client.post(
                f"/api/v2/workspaces/{ws}/vars",
                json=_var_body("TOKEN", f"v-{flag}", apply_only=flag),
                headers=AUTH,
            )
            assert resp.status_code == 201, resp.text
            ids[flag] = resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/workspaces/{ws}/vars/{ids[False]}",
            json={"data": {"type": "vars", "attributes": {"apply-only": True}}},
            headers=AUTH,
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "TOKEN" in detail and "an apply-only variable" in detail, (
            f"the held slot is the apply-only one here: {detail!r}"
        )

    async def test_a_create_collision_names_the_slot_too(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"create-{uuid.uuid4().hex[:8]}")
        first = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("DUP", "one", apply_only=True),
            headers=AUTH,
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("DUP", "two", apply_only=True),
            headers=AUTH,
        )
        assert second.status_code == 409
        detail = second.json()["detail"]
        assert "DUP" in detail and "an apply-only variable" in detail, detail

    async def test_a_variable_set_edit_conflict_says_variable_set(self, app, client):
        set_auth(app, admin_user())
        created = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {"name": f"vs-{uuid.uuid4().hex[:8]}"},
                }
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        vs = created.json()["data"]["id"]
        ids = {}
        for flag in (False, True):
            resp = await client.post(
                f"/api/v2/varsets/{vs}/relationships/vars",
                json=_var_body("SHARED", f"v-{flag}", apply_only=flag),
                headers=AUTH,
            )
            assert resp.status_code == 201, resp.text
            ids[flag] = resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/varsets/{vs}/relationships/vars/{ids[True]}",
            json={"data": {"type": "vars", "attributes": {"apply-only": False}}},
            headers=AUTH,
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "SHARED" in detail, detail
        assert "variable set" in detail, (
            f"the scope must say variable set, not workspace: {detail!r}"
        )


class TestResolutionAgainstRealRows:
    async def test_the_phases_get_different_values_for_one_key(self, app, client):
        set_auth(app, admin_user())
        ws = await _seed_workspace("res")
        async with get_db_session() as db:
            db.add(Variable(workspace_id=ws, key="ROLE", value="plan-role", category="env"))
            db.add(
                Variable(
                    workspace_id=ws,
                    key="ROLE",
                    value="apply-role",
                    category="env",
                    apply_only=True,
                )
            )
            await db.commit()

        async with get_db_session() as db:
            plan = _by_key(await resolve_variables(db, ws, phase="plan"))
            apply = _by_key(await resolve_variables(db, ws, phase="apply"))

        assert plan["ROLE"].value == "plan-role"
        assert apply["ROLE"].value == "apply-role", (
            "the apply-only row must overlay its standard twin on an apply"
        )

    async def test_an_apply_only_key_is_absent_from_a_plan_not_empty(self, app, client):
        set_auth(app, admin_user())
        ws = await _seed_workspace("solo")
        async with get_db_session() as db:
            db.add(
                Variable(
                    workspace_id=ws,
                    key="DEPLOY_KEY",
                    value="secret",
                    category="env",
                    apply_only=True,
                )
            )
            await db.commit()

        async with get_db_session() as db:
            plan = _by_key(await resolve_variables(db, ws, phase="plan"))
            apply = _by_key(await resolve_variables(db, ws, phase="apply"))

        assert "DEPLOY_KEY" not in plan, (
            "an empty value reaches terraform as a DECLARED variable, which is not "
            "the same as an undeclared one -- the key must be absent"
        )
        assert apply["DEPLOY_KEY"].value == "secret"

    async def test_a_priority_set_standard_row_still_beats_a_workspace_apply_only_row(
        self, app, client
    ):
        """The inversion the two-pass design would have shipped.

        A priority variable set exists so a platform team can impose a value a
        workspace owner cannot override. Overlaying apply-only rows across the
        whole chain rather than within each tier would hand that authority to
        anyone with workspace write access, on apply runs only -- visible to
        nobody until an apply used the wrong value.
        """
        tag = uuid.uuid4().hex[:8]
        async with get_db_session() as db:
            ws = Workspace(name=f"prio-{tag}")
            db.add(ws)
            await db.flush()

            vs = VariableSet(name=f"prio-set-{tag}", priority=True)
            db.add(vs)
            await db.flush()
            db.add(
                VariableSetVariable(
                    variable_set_id=vs.id, key="ROLE", value="imposed", category="env"
                )
            )
            db.add(VariableSetWorkspace(variable_set_id=vs.id, workspace_id=ws.id))

            # The workspace tries to win an apply with an apply-only row.
            db.add(
                Variable(
                    workspace_id=ws.id,
                    key="ROLE",
                    value="escalated",
                    category="env",
                    apply_only=True,
                )
            )
            await db.commit()
            ws_id = ws.id

        async with get_db_session() as db:
            apply = _by_key(await resolve_variables(db, ws_id, phase="apply"))

        assert apply["ROLE"].value == "imposed", (
            "a workspace apply-only row must not outrank a priority set's standard "
            "row; the overlay is within a tier, never across the chain"
        )

    async def test_a_phase_blind_resolution_keeps_both_rows(self, app, client):
        """The redaction caller needs every sensitive literal, not one per key.

        `summariser` resolves with no phase so it can scrub both values out of a
        prompt. Collapsing to one row per key there would leak whichever value
        lost.
        """
        set_auth(app, admin_user())
        ws = await _seed_workspace("blind")
        async with get_db_session() as db:
            for value, flag in (("plan-secret", False), ("apply-secret", True)):
                db.add(
                    Variable(
                        workspace_id=ws,
                        key="TOKEN",
                        value=value,
                        category="env",
                        sensitive=True,
                        apply_only=flag,
                    )
                )
            await db.commit()

        async with get_db_session() as db:
            everything = await resolve_variables(db, ws)

        values = {v.value for v in everything if v.key == "TOKEN"}
        assert values == {"plan-secret", "apply-secret"}, (
            f"phase-blind resolution must keep both rows so both can be redacted; got {values}"
        )


class TestTheDefaultIsInvisible:
    async def test_a_workspace_with_no_apply_only_rows_resolves_identically(self, app, client):
        """Every existing workspace must be untouched by this feature."""
        set_auth(app, admin_user())
        ws = await _seed_workspace("plain")
        async with get_db_session() as db:
            for key in ("A", "B", "C"):
                db.add(Variable(workspace_id=ws, key=key, value=f"v-{key}", category="env"))
            await db.commit()

        async with get_db_session() as db:
            plan = _by_key(await resolve_variables(db, ws, phase="plan"))
            apply = _by_key(await resolve_variables(db, ws, phase="apply"))

        assert {k: v.value for k, v in plan.items()} == {"A": "v-A", "B": "v-B", "C": "v-C"}
        assert {k: v.value for k, v in apply.items()} == {k: v.value for k, v in plan.items()}

    async def test_the_column_defaults_to_false_at_the_database(self, app, client):
        """A row written by an older replica mid-rolling-upgrade carries false."""
        set_auth(app, admin_user())
        ws = await _seed_workspace("default")
        async with get_db_session() as db:
            db.add(Variable(workspace_id=ws, key="X", value="x", category="env"))
            await db.commit()

        async with get_db_session() as db:
            row = (
                await db.execute(select(Variable).where(Variable.workspace_id == ws))
            ).scalar_one()
            assert row.apply_only is False


class TestTheThirdSlot:
    """Configure-only rows, against a real database (#2190).

    At this tier because every property here is one the unit tiers cannot see:
    the uniqueness constraint is enforced by Postgres and this is the only tier
    whose schema comes from running the migration chain rather than from
    `Base.metadata`, which is exactly how #2189 came to be dead at the database
    level while a green suite ran over four-column models.
    """

    async def test_one_key_is_held_in_ALL_THREE_slots(self, app, client):
        """The whole point of widening the tuple to five columns.

        Three rows, one key. A test using two would pass with the constraint
        still at four columns, because the pair it tests is the one #2189
        already permitted.
        """
        set_auth(app, admin_user())
        ws = await _workspace(client, f"trio-{uuid.uuid4().hex[:8]}")
        for body in (
            _var_body("TOKEN", "ordinary", apply_only=False),
            _var_body("TOKEN", "for-apply", apply_only=True),
            _var_body("TOKEN", "for-configure", apply_only=False, configure_only=True),
        ):
            resp = await client.post(f"/api/v2/workspaces/{ws}/vars", json=body, headers=AUTH)
            assert resp.status_code == 201, resp.text

    async def test_a_FOURTH_row_in_an_occupied_slot_is_still_refused(self, app, client):
        """The constraint must still forbid a genuine duplicate. Widening it is
        permissive, and a widening that went too far -- or a nullable column,
        which Postgres treats as distinct -- would silently stop catching any
        duplicate at all while the constraint still appeared to be there."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"dup3-{uuid.uuid4().hex[:8]}")
        body = _var_body("TOKEN", "first", apply_only=False, configure_only=True)
        assert (
            await client.post(f"/api/v2/workspaces/{ws}/vars", json=body, headers=AUTH)
        ).status_code == 201
        again = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("TOKEN", "second", apply_only=False, configure_only=True),
            headers=AUTH,
        )
        assert again.status_code == 409, again.text
        assert "configure-only" in again.json()["errors"][0]["detail"]

    async def test_both_flags_together_are_refused_at_the_write(self, app, client):
        """422, not a resolution. Two independent booleans make the combination
        representable, and neither reading of it is one anybody would predict:
        the union is wider than either flag, which is the opposite of what a
        withholding flag is for, and the intersection is what configure-only
        already says alone."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"both-{uuid.uuid4().hex[:8]}")
        resp = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("TOKEN", "v", apply_only=True, configure_only=True),
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["errors"][0]["detail"]
        assert "apply-only" in detail and "configure-only" in detail

    async def test_a_PATCH_that_creates_the_combination_is_refused_too(self, app, client):
        """The edit a request-only guard would miss: setting one flag against a
        row that already carries the other. The guard reads the MERGED pair."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"merge-{uuid.uuid4().hex[:8]}")
        created = await client.post(
            f"/api/v2/workspaces/{ws}/vars",
            json=_var_body("TOKEN", "v", apply_only=True),
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        var_id = created.json()["data"]["id"]
        resp = await client.patch(
            f"/api/v2/workspaces/{ws}/vars/{var_id}",
            json={"data": {"type": "vars", "attributes": {"configure-only": True}}},
            headers=AUTH,
        )
        assert resp.status_code == 422, resp.text

    async def test_each_phase_gets_its_own_value_for_one_key(self, app, client):
        """The delivery property, all three phases on one fixture.

        Stated as a cross product rather than three separate assertions: each
        alone passes whether or not the new flag is in the filter, because a
        plan excludes it either way and a configure includes apply-only either
        way. It is the three together that pin which slot each phase gets.
        """
        set_auth(app, admin_user())
        ws = await _workspace(client, f"phases-{uuid.uuid4().hex[:8]}")
        for body in (
            _var_body("TOKEN", "ordinary", apply_only=False),
            _var_body("TOKEN", "for-apply", apply_only=True),
            _var_body("TOKEN", "for-configure", apply_only=False, configure_only=True),
        ):
            assert (
                await client.post(f"/api/v2/workspaces/{ws}/vars", json=body, headers=AUTH)
            ).status_code == 201

        ws_id = uuid.UUID(ws.removeprefix("ws-"))
        async with get_db_session() as db:
            plan = _by_key(await resolve_variables(db, ws_id, phase="plan"))
            apply = _by_key(await resolve_variables(db, ws_id, phase="apply"))
            configure = _by_key(await resolve_variables(db, ws_id, phase="configure"))

        assert plan["TOKEN"].value == "ordinary", "a plan must see neither withheld row"
        assert apply["TOKEN"].value == "for-apply", (
            "an apply sees apply-only and must NOT see configure-only"
        )
        assert configure["TOKEN"].value == "for-configure", (
            "a configure sees all three and the narrowest wins"
        )

    async def test_a_configure_only_key_is_absent_from_an_apply_not_empty(self, app, client):
        """An empty value reaches terraform as a DECLARED variable, which is a
        different thing from an undeclared one -- so the row must be absent
        rather than present-and-blank. The same property #2189 pinned for a
        plan, one slot over."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"absent-{uuid.uuid4().hex[:8]}")
        assert (
            await client.post(
                f"/api/v2/workspaces/{ws}/vars",
                json=_var_body("SSH_KEY", "secret", apply_only=False, configure_only=True),
                headers=AUTH,
            )
        ).status_code == 201
        ws_id = uuid.UUID(ws.removeprefix("ws-"))
        async with get_db_session() as db:
            apply = _by_key(await resolve_variables(db, ws_id, phase="apply"))
            plan = _by_key(await resolve_variables(db, ws_id, phase="plan"))
        assert "SSH_KEY" not in apply
        assert "SSH_KEY" not in plan

    async def test_a_phase_blind_resolution_keeps_ALL_THREE_rows(self, app, client):
        """Prompt redaction is the only caller passing no phase, and it must see
        every half of the family or a secret that could reach the model cannot
        be scrubbed. With `configure_only` missing from the phase-blind key, an
        ordinary row and the fleet's credential of the same key collapse to one
        value -- the exact defect #2192 fixed for `apply_only`."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"blind-{uuid.uuid4().hex[:8]}")
        for body in (
            _var_body("TOKEN", "ordinary", apply_only=False),
            _var_body("TOKEN", "for-apply", apply_only=True),
            _var_body("TOKEN", "for-configure", apply_only=False, configure_only=True),
        ):
            assert (
                await client.post(f"/api/v2/workspaces/{ws}/vars", json=body, headers=AUTH)
            ).status_code == 201
        ws_id = uuid.UUID(ws.removeprefix("ws-"))
        async with get_db_session() as db:
            everything = await resolve_variables(db, ws_id, phase=None)
        values = sorted(v.value for v in everything if v.key == "TOKEN")
        assert values == ["for-apply", "for-configure", "ordinary"], (
            f"phase-blind resolution dropped a row: {values}"
        )

    async def test_a_priority_set_standard_row_still_beats_a_workspace_configure_only_row(
        self, app, client
    ):
        """The precedence inversion that must not ship, extended to the third
        slot. Resolution is within a tier, never a pass over the chain -- a pass
        would let a workspace row beat a priority variable set's and invert the
        authority order those sets exist to establish, on the one phase that
        reaches real hosts."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"prio-{uuid.uuid4().hex[:8]}")
        ws_id = uuid.UUID(ws.removeprefix("ws-"))
        assert (
            await client.post(
                f"/api/v2/workspaces/{ws}/vars",
                json=_var_body(
                    "TOKEN", "workspace-configure", apply_only=False, configure_only=True
                ),
                headers=AUTH,
            )
        ).status_code == 201

        vs = await client.post(
            VARSET_ENDPOINT,
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {
                        "name": f"prio-{uuid.uuid4().hex[:8]}",
                        "global": True,
                        "priority": True,
                    },
                }
            },
            headers=AUTH,
        )
        assert vs.status_code == 201, vs.text
        vs_id = vs.json()["data"]["id"]
        assert (
            await client.post(
                f"/api/v2/varsets/{vs_id}/relationships/vars",
                json=_var_body("TOKEN", "priority-standard", apply_only=False),
                headers=AUTH,
            )
        ).status_code == 201

        async with get_db_session() as db:
            configure = _by_key(await resolve_variables(db, ws_id, phase="configure"))
        assert configure["TOKEN"].value == "priority-standard", (
            "a workspace configure-only row beat a priority set's standard row, which "
            "inverts the authority order priority sets exist to establish"
        )

    async def test_the_column_defaults_to_false_at_the_database(self, app, client):
        """Not a Python-side default. Postgres treats NULLs as distinct in a
        unique constraint, so a nullable column left NULL on pre-migration rows
        would silently stop both constraints catching duplicates for exactly
        those rows, while the constraint still appeared to be there."""
        async with get_db_session() as db:
            row = await db.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'variables' AND column_name = 'configure_only'"
                )
            )
            nullable, default = row.one()
        assert nullable == "NO", "a nullable flag disables the uniqueness constraint it joins"
        assert default is not None and "false" in default.lower(), (
            f"the backfill must be in the database, not in Python: {default!r}"
        )
