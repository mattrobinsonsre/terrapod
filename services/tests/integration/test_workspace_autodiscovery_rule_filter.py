"""Integration tests for selecting workspaces by `autodiscovery_rule_id` (real Postgres)."""

import uuid

import pytest

from terrapod.db.models import AutodiscoveryRule, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

SEARCH = "/api/terrapod/v1/workspaces/actions/search"


async def _seed(tag):
    """Two workspaces from rule A, one from rule B and one made by hand."""
    async with get_db_session() as db:
        conn = VCSConnection(
            provider="github",
            name=f"conn-{tag}",
            server_url="",
            token="x",
            status="active",
            github_installation_id=int(tag[:7], 16),
        )
        db.add(conn)
        await db.flush()
        rules = [
            AutodiscoveryRule(
                vcs_connection_id=conn.id,
                repo_url="https://github.com/acme/repo",
                pattern=f"{name}/*/",
                name=f"{name}-{tag}",
            )
            for name in ("a", "b")
        ]
        db.add_all(rules)
        await db.flush()
        rule_a, rule_b = rules
        for name, rule in (("a1", rule_a), ("a2", rule_a), ("b1", rule_b), ("hand", None)):
            db.add(
                Workspace(
                    name=f"adr-{tag}-{name}",
                    labels={"grp": tag},
                    autodiscovery_rule_id=rule.id if rule else None,
                )
            )
        await db.commit()
        return rule_a.id, rule_b.id


async def _names(client, filter_):
    resp = await client.post(SEARCH, json={"filter": filter_}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return sorted(w["name"].split("-", 2)[2] for w in resp.json()["workspaces"])


class TestAutodiscoveryRuleSelector:
    async def test_matches_only_workspaces_created_by_that_rule(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        rule_a, rule_b = await _seed(tag)

        assert await _names(client, {"autodiscovery-rule-id": str(rule_a)}) == ["a1", "a2"]
        assert await _names(client, {"autodiscovery_rule_id": str(rule_b)}) == ["b1"]

    async def test_combines_with_other_dimensions(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        rule_a, _ = await _seed(tag)

        selector = {"autodiscovery-rule-id": str(rule_a), "labels": {"grp": tag}}
        assert await _names(client, selector) == ["a1", "a2"]

    async def test_a_rule_with_no_workspaces_matches_nothing(self, app, client):
        set_auth(app, admin_user())

        assert await _names(client, {"autodiscovery-rule-id": str(uuid.uuid4())}) == []

    async def test_exclude_removes_the_rules_workspaces(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        rule_a, _ = await _seed(tag)

        selector = {
            "labels": {"grp": tag},
            "exclude": [{"autodiscovery-rule-id": str(rule_a)}],
        }
        assert await _names(client, selector) == ["b1", "hand"]

    async def test_an_invalid_id_is_422(self, app, client):
        set_auth(app, admin_user())

        resp = await client.post(
            SEARCH, json={"filter": {"autodiscovery-rule-id": "nope"}}, headers=AUTH
        )
        assert resp.status_code == 422, resp.text
