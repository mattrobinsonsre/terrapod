"""Integration tests for `propagate` and `dry_run` on the rule PATCH (#2549), through the real route."""

import pytest
from sqlalchemy import select

from terrapod.db.models import AutodiscoveryRule, Run
from terrapod.db.session import get_db_session
from tests.integration.conftest import AUTH, admin_user, regular_user, set_auth
from tests.integration.test_rule_propagation_integration import _audit_rows, _seed, _workspaces

pytestmark = pytest.mark.integration

URL = "/api/terrapod/v1/autodiscovery-rules/{}"


async def _patch(client, app, rule_id, attributes, options=None, request_id=None, user=None):
    set_auth(app, user or admin_user())
    headers = {**AUTH, **({"X-Request-ID": request_id} if request_id else {})}
    body = {"data": {"attributes": attributes}, **(options or {})}
    return await client.patch(URL.format(rule_id), json=body, headers=headers)


async def _rule(rule_id) -> AutodiscoveryRule:
    async with get_db_session() as db:
        return await db.get(AutodiscoveryRule, rule_id)


def _short(entries) -> list[str]:
    return sorted(e["name"].split("-", 1)[1] for e in entries)


RULE = {"engine_version": "1.8"}
WORKSPACES = {"stale": {"engine_version": "1.8"}, "own": {"engine_version": "1.5"}}


class TestPropagateOnPatch:
    async def test_apply_updates_matching_workspaces_and_the_rule(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(client, app, rule_id, {"engine-version": "1.9"}, {"propagate": True})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["attributes"]["engine-version"] == "1.9"
        meta = body["meta"]["propagation"]
        assert (meta["dry_run"], meta["matched"], meta["skipped_lifecycle"]) == (False, 2, 0)
        assert _short(meta["would_change"]) == ["stale"]
        assert _short(meta["customised"]) == ["own"]
        ws = await _workspaces(rule_id)
        assert (ws["stale"].engine_version, ws["own"].engine_version) == ("1.9", "1.5")
        assert (await _rule(rule_id)).engine_version == "1.9"

    async def test_dry_run_previews_and_persists_neither_rule_nor_workspaces(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(
            client,
            app,
            rule_id,
            {"engine-version": "1.9", "pattern": "envs/*/*.tf"},
            {"propagate": True, "dry_run": True},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["attributes"]["engine-version"] == "1.9"
        assert body["data"]["attributes"]["pattern"] == "envs/*/*.tf"
        assert body["meta"]["propagation"]["dry_run"] is True
        assert _short(body["meta"]["propagation"]["would_change"]) == ["stale"]
        rule = await _rule(rule_id)
        assert (rule.engine_version, rule.pattern) == ("1.8", "stacks/*/*.tf")
        ws = await _workspaces(rule_id)
        assert (ws["stale"].engine_version, ws["own"].engine_version) == ("1.8", "1.5")
        assert await _audit_rows() == []

    async def test_a_guard_refusal_is_422_and_leaves_the_rule_unchanged(self, app, client):
        rule_id = await _seed(
            {"a-tf": {"engine_version": "1.8"}, "b-pulumi": {"engine": "pulumi"}},
            rule_columns={**RULE, "security_scan_enforcement": "advisory"},
        )

        resp = await _patch(
            client,
            app,
            rule_id,
            {"engine-version": "1.9", "security-scan-enforcement": "enforced"},
            {"propagate": True},
        )

        assert resp.status_code == 422, resp.text
        assert "b-pulumi" in resp.json()["detail"]
        rule = await _rule(rule_id)
        assert (rule.engine_version, rule.security_scan_enforcement) == ("1.8", "advisory")
        ws = await _workspaces(rule_id)
        assert ws["a-tf"].engine_version == "1.8"
        assert await _audit_rows() == []

    async def test_a_run_kind_refusal_is_422_and_leaves_the_rule_unchanged(self, app, client):
        rule_id = await _seed(
            {"own": {"configure_enabled": True, "default_run_kind": "configure"}},
            rule_columns={"configure_enabled": True, "default_run_kind": "plan_apply"},
        )

        resp = await _patch(
            client,
            app,
            rule_id,
            {"configure-enabled": False, "default-run-kind": "plan"},
            {"propagate": True},
        )

        assert resp.status_code == 422, resp.text
        assert "own" in resp.json()["detail"]
        rule = await _rule(rule_id)
        assert (rule.configure_enabled, rule.default_run_kind) == (True, "plan_apply")
        ws = (await _workspaces(rule_id))["own"]
        assert (ws.configure_enabled, ws.default_run_kind) == (True, "configure")

    async def test_without_options_a_patch_touches_no_workspace(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(client, app, rule_id, {"engine-version": "1.9"})

        assert resp.status_code == 200, resp.text
        assert list(resp.json()) == ["data"]
        assert (await _rule(rule_id)).engine_version == "1.9"
        ws = await _workspaces(rule_id)
        assert (ws["stale"].engine_version, ws["own"].engine_version) == ("1.8", "1.5")
        assert await _audit_rows() == []

    @pytest.mark.parametrize("options", [{"propagate": True}, {"propagate": True, "dry_run": True}])
    async def test_a_non_admin_is_403_and_nothing_changes(self, app, client, options):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(
            client, app, rule_id, {"engine-version": "1.9"}, options, user=regular_user()
        )

        assert resp.status_code == 403, resp.text
        assert (await _rule(rule_id)).engine_version == "1.8"
        ws = await _workspaces(rule_id)
        assert (ws["stale"].engine_version, ws["own"].engine_version) == ("1.8", "1.5")
        assert await _audit_rows() == []

    async def test_dry_run_alone_is_422_and_changes_nothing(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(client, app, rule_id, {"engine-version": "1.9"}, {"dry_run": True})

        assert resp.status_code == 422, resp.text
        assert (await _rule(rule_id)).engine_version == "1.8"

    async def test_per_workspace_audit_rows_carry_the_request_id(self, app, client):
        rule_id = await _seed(
            {"a": {"engine_version": "1.8"}, "b": {"engine_version": "1.8"}}, rule_columns=RULE
        )

        resp = await _patch(
            client, app, rule_id, {"engine-version": "1.9"}, {"propagate": True}, "req-patch-1"
        )

        assert resp.status_code == 200, resp.text
        rows = await _audit_rows()
        assert len(rows) == 2
        assert {r.request_id for r in rows} == {"req-patch-1"}
        assert {r.actor_email for r in rows} == {"admin@test.com"}

    async def test_the_input_alias_is_propagated_once_as_the_stored_spelling(self, app, client):
        rule_id = await _seed({"stale": {"engine_version": "1.8"}}, rule_columns=RULE)

        resp = await _patch(client, app, rule_id, {"terraform-version": "1.9"}, {"propagate": True})

        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]["propagation"]
        [entry] = meta["would_change"]
        assert entry["diff"] == {"engine_version": {"from": "1.8", "to": "1.9"}}
        assert (await _workspaces(rule_id))["stale"].engine_version == "1.9"
        [row] = await _audit_rows()
        assert row.detail.endswith("['engine_version']")

    async def test_a_change_to_no_propagated_field_succeeds_with_nothing_changed(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        resp = await _patch(client, app, rule_id, {"pattern": "envs/*/*.tf"}, {"propagate": True})

        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]["propagation"]
        assert (meta["matched"], meta["would_change"], meta["customised"]) == (2, [], [])
        assert (await _rule(rule_id)).pattern == "envs/*/*.tf"
        assert await _audit_rows() == []

    async def test_propagation_creates_no_run(self, app, client):
        rule_id = await _seed(WORKSPACES, rule_columns=RULE)

        await _patch(client, app, rule_id, {"engine-version": "1.9"}, {"propagate": True})

        async with get_db_session() as db:
            assert (await db.execute(select(Run))).first() is None
