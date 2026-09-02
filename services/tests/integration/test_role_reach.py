"""Role reach preview (#1456) — which workspaces a role grants on, and why.

Real DB throughout, because the point of the feature is the SQL: allow and deny
are both evaluated as JSONB containment / name membership so counts stay
aggregate at fleet scale. A mocked session would assert the shape of a query
nobody ran and prove nothing about whether it selects the right rows.
"""

import uuid

import pytest

from terrapod.db.models import Role
from terrapod.db.session import get_db_session
from terrapod.services import role_reach_service
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


@pytest.fixture(autouse=True)
def _as_admin(app):
    """Every test here runs as a platform admin: the preview is gated on
    admin/audit, and the feature under test is the reach calculation, not the
    gate (which has its own test below)."""
    set_auth(app, admin_user())


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


async def _role(client, name, **attrs):
    resp = await client.post(
        "/api/terrapod/v1/roles",
        json={"data": {"attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return name


class TestReachMatchesEnforcement:
    """The preview must agree with the gate that actually authorises, because a
    permissions view that disagrees with enforcement is worse than none."""

    async def test_allow_label_reaches_exactly_the_labelled_workspaces(self, app, client):
        tag = uuid.uuid4().hex[:8]
        hit = await _ws(client, f"reach-hit-{tag}", labels={"env": f"prod{tag}"})
        await _ws(client, f"reach-miss-{tag}", labels={"env": f"dev{tag}"})
        await _role(
            client,
            f"reach-role-{tag}",
            **{"allow-labels": {"env": [f"prod{tag}"]}, "workspace-permission": "write"},
        )

        resp = await client.get(f"/api/terrapod/v1/roles/reach-role-{tag}/preview", headers=AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]

        assert attrs["granted-count"] == 1
        assert attrs["denied-count"] == 0
        names = [w["name"] for w in attrs["workspaces"]]
        assert names == [f"reach-hit-{tag}"]
        assert attrs["workspaces"][0]["id"] == hit
        assert attrs["workspaces"][0]["reason"] == f"allow-label:env=prod{tag}"
        # The capability set is the role's own, sliced to the workspace axis —
        # not a level name, since levels are authoring shorthand only.
        # `run:apply` is write-only, so its presence proves the role's grant is
        # reported rather than some read floor.
        assert "run:apply" in attrs["workspaces"][0]["capabilities"]

    async def test_the_verdict_agrees_with_the_enforcement_gate(self, app, client):
        """Same rule, both paths, every workspace — the equivalence that stops
        the preview drifting from `_role_matches`."""
        from terrapod.services.capability_resolver import MATCH_ALLOWED, _role_matches

        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"agree-a-{tag}", labels={"team": f"net{tag}"})
        await _ws(client, f"agree-b-{tag}", labels={"team": f"net{tag}", "tier": "x"})
        await _ws(client, f"agree-c-{tag}", labels={"team": f"other{tag}"})
        await _role(
            client,
            f"agree-role-{tag}",
            **{
                "allow-labels": {"team": [f"net{tag}"]},
                "deny-labels": {"tier": ["x"]},
                "workspace-permission": "read",
            },
        )

        async with get_db_session() as db:
            role = await db.get(Role, f"agree-role-{tag}")
            result = await role_reach_service.preview_role_reach(db, role, limit=100)
            from sqlalchemy import select

            from terrapod.db.models import Workspace

            everything = (await db.execute(select(Workspace))).scalars().all()
            for ws in everything:
                granted = any(w["id"] == f"ws-{ws.id}" for w in result["workspaces"])
                assert granted == _role_matches(role, ws.name, ws.labels or {}), (
                    f"preview and enforcement disagree on {ws.name}"
                )
                if granted:
                    entry = next(w for w in result["workspaces"] if w["id"] == f"ws-{ws.id}")
                    assert entry["verdict"] == MATCH_ALLOWED


class TestDenyIsShownNotSilentlyOmitted:
    """ "matched 47, denied 3" is what makes a deny rule safe to write. An
    operator who cannot see what a deny removed cannot tell an intended
    exclusion from a typo."""

    async def test_denied_workspaces_are_reported_separately(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"deny-keep-{tag}", labels={"env": f"prod{tag}"})
        await _ws(client, f"deny-drop-{tag}", labels={"env": f"prod{tag}", "locked-down": "yes"})
        await _role(
            client,
            f"deny-role-{tag}",
            **{
                "allow-labels": {"env": [f"prod{tag}"]},
                "deny-labels": {"locked-down": ["yes"]},
                "workspace-permission": "write",
            },
        )

        resp = await client.get(f"/api/terrapod/v1/roles/deny-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]

        assert attrs["granted-count"] == 1
        assert attrs["denied-count"] == 1
        assert attrs["matched-count"] == 2
        assert [w["name"] for w in attrs["workspaces"]] == [f"deny-keep-{tag}"]
        assert [w["name"] for w in attrs["denied"]] == [f"deny-drop-{tag}"]
        assert attrs["denied"][0]["reason"] == "deny-label:locked-down=yes"
        # Denied means denied: no capabilities are reported for it.
        assert attrs["denied"][0]["capabilities"] == []

    async def test_deny_name_beats_an_allow_label(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"dn-{tag}", labels={"env": f"prod{tag}"})
        await _role(
            client,
            f"dn-role-{tag}",
            **{
                "allow-labels": {"env": [f"prod{tag}"]},
                "deny-names": [f"dn-{tag}"],
                "workspace-permission": "admin",
            },
        )
        resp = await client.get(f"/api/terrapod/v1/roles/dn-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 0
        assert attrs["denied-count"] == 1
        assert attrs["denied"][0]["reason"] == "deny-name"


class TestEmptyRulesReachNothing:
    """An empty allow set matches NOTHING, which is not the same as matching
    everything. Conflating the two would silently report a role as granting the
    entire estate."""

    async def test_a_role_with_no_allow_rules_reaches_nothing(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"empty-{tag}", labels={"env": "prod"})
        await _role(client, f"empty-role-{tag}", **{"workspace-permission": "admin"})
        resp = await client.get(f"/api/terrapod/v1/roles/empty-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 0
        assert attrs["workspaces"] == []


class TestUnsavedPreview:
    """The authoring case: see the match while typing, before the rule exists."""

    async def test_an_unsaved_body_previews_without_persisting(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"uns-{tag}", labels={"squad": f"blue{tag}"})

        resp = await client.post(
            "/api/terrapod/v1/roles/preview",
            json={
                "data": {
                    "attributes": {
                        "name": f"never-saved-{tag}",
                        "allow-labels": {"squad": [f"blue{tag}"]},
                        "workspace-permission": "plan",
                    }
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["granted-count"] == 1

        # Nothing was persisted — a preview that created the role would be a
        # write dressed as a read.
        async with get_db_session() as db:
            assert await db.get(Role, f"never-saved-{tag}") is None

    async def test_an_invalid_rule_is_rejected_the_same_as_on_save(self, app, client):
        """A preview that accepts what a save rejects answers about a role that
        cannot exist."""
        resp = await client.post(
            "/api/terrapod/v1/roles/preview",
            json={"data": {"attributes": {"name": "bad", "workspace-permission": "sudo"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422


class TestBuiltinsAndMissing:
    async def test_a_builtin_role_is_refused_rather_than_answered(self, app, client):
        """`admin` grants through the platform path on every workspace, so a
        label-reach answer would be true and deeply misleading."""
        resp = await client.get("/api/terrapod/v1/roles/admin/preview", headers=AUTH)
        assert resp.status_code == 422
        assert "built-in" in resp.json()["detail"]

    async def test_unknown_role_404s(self, app, client):
        resp = await client.get("/api/terrapod/v1/roles/nope-does-not-exist/preview", headers=AUTH)
        assert resp.status_code == 404


class TestPaging:
    """Counts are aggregates over the fleet, not over the page — an operator
    needs "this reaches 4,200" to be true, not truncated to what fitted."""

    async def test_counts_span_the_fleet_while_the_page_is_bounded(self, app, client):
        tag = uuid.uuid4().hex[:8]
        for i in range(5):
            await _ws(client, f"page-{tag}-{i}", labels={"grp": tag})
        await _role(
            client,
            f"page-role-{tag}",
            **{"allow-labels": {"grp": [tag]}, "workspace-permission": "read"},
        )

        resp = await client.get(
            f"/api/terrapod/v1/roles/page-role-{tag}/preview?page[size]=2", headers=AUTH
        )
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 5
        assert len(attrs["workspaces"]) == 2

        resp2 = await client.get(
            f"/api/terrapod/v1/roles/page-role-{tag}/preview?page[size]=2&page[number]=3",
            headers=AUTH,
        )
        assert len(resp2.json()["data"]["attributes"]["workspaces"]) == 1
