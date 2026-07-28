"""JSON:API links live in `relationships` (#1063 slice 2).

The house style says a link to another resource is a JSON:API *relationship*.
Historically several serializers only emitted a `*-id` **attribute**. Slice 2
adds the relationship alongside, additively — the attribute stays indefinitely
for back-compat.

These tests call the serializers directly (no DB) and assert BOTH forms are
present and agree, so a future edit can't drop either half silently.
"""

import uuid
from types import SimpleNamespace

from terrapod.api.routers.autodiscovery_rules import _rule_json
from terrapod.api.routers.catalog import _instance_json
from terrapod.api.routers.policy_sets import _policy_set_json
from terrapod.api.routers.registry_modules import _link_to_jsonapi


def _rel_id(doc: dict, name: str) -> str | None:
    return doc.get("relationships", {}).get(name, {}).get("data", {}).get("id")


class TestCatalogInstance:
    def _ws(self, *, pool: uuid.UUID | None, item: uuid.UUID | None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            name="inst",
            catalog_item_id=item,
            catalog_version_pin="1.0.0",
            agent_pool_id=pool,
            owner_email="a@b.c",
            labels={},
        )

    def test_agent_pool_in_both_attribute_and_relationship(self):
        pool = uuid.uuid4()
        doc = _instance_json(self._ws(pool=pool, item=uuid.uuid4()))
        assert doc["attributes"]["agent-pool-id"] == f"apool-{pool}"
        assert _rel_id(doc, "agent-pool") == f"apool-{pool}"
        assert doc["relationships"]["agent-pool"]["data"]["type"] == "agent-pools"

    def test_workspace_relationship_always_present(self):
        ws = self._ws(pool=None, item=None)
        doc = _instance_json(ws)
        assert _rel_id(doc, "workspace") == f"ws-{ws.id}"

    def test_absent_links_omit_relationship_entirely(self):
        doc = _instance_json(self._ws(pool=None, item=None))
        assert doc["attributes"]["agent-pool-id"] is None
        assert "agent-pool" not in doc["relationships"]
        assert "catalog-item" not in doc["relationships"]


class TestAutodiscoveryRule:
    def _rule(self, *, pool: uuid.UUID | None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            name="r",
            name_template="",
            vcs_connection_id=uuid.uuid4(),
            repo_url="https://example.com/org/repo",
            branch="main",
            pattern="**",
            ignore_patterns=[],
            enabled=True,
            execution_mode="agent",
            execution_backend="tofu",
            agent_pool_id=pool,
            terraform_version="1.12",
            resource_cpu="1",
            resource_memory="2Gi",
            auto_apply=False,
            on_directory_delete="flag",
            labels={},
            owner_email="",
            var_files=[],
            run_task_templates=[],
            notification_templates=[],
            execution_hook_templates=[],
            created_at=None,
            updated_at=None,
        )

    def test_vcs_connection_and_agent_pool_relationships(self):
        rule = self._rule(pool=uuid.uuid4())
        doc = _rule_json(rule)
        assert _rel_id(doc, "vcs-connection") == f"vcs-{rule.vcs_connection_id}"
        assert _rel_id(doc, "agent-pool") == f"apool-{rule.agent_pool_id}"
        # attributes retained (back-compat)
        assert doc["attributes"]["vcs-connection-id"] == str(rule.vcs_connection_id)
        assert doc["attributes"]["agent-pool-id"] == str(rule.agent_pool_id)

    def test_no_pool_omits_agent_pool_relationship(self):
        doc = _rule_json(self._rule(pool=None))
        assert "agent-pool" not in doc["relationships"]
        assert _rel_id(doc, "vcs-connection") is not None


class TestPolicySet:
    def _ps(self, *, vcs: uuid.UUID | None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            name="ps",
            description="",
            enforcement_level="advisory",
            enabled=True,
            global_scope=True,
            allow_labels={},
            allow_names=[],
            deny_labels={},
            deny_names=[],
            source="api",
            vcs_connection_id=vcs,
            vcs_repo_url=None,
            vcs_branch=None,
            policy_path=None,
            vcs_last_commit_sha=None,
            vcs_last_synced_at=None,
            vcs_last_error=None,
            policies=[],
            created_by="",
            created_at=None,
            updated_at=None,
        )

    def test_vcs_relationship_added_when_connected(self):
        ps = self._ps(vcs=uuid.uuid4())
        doc = _policy_set_json(ps)
        assert doc["attributes"]["vcs-connection-id"] == f"vcs-{ps.vcs_connection_id}"
        assert _rel_id(doc, "vcs-connection") == f"vcs-{ps.vcs_connection_id}"

    def test_api_sourced_set_has_no_relationships_key(self):
        # No VCS link and no embedded policies → nothing to relate; the key is
        # omitted rather than emitted empty.
        doc = _policy_set_json(self._ps(vcs=None))
        assert "relationships" not in doc

    def test_embedded_policies_still_work_alongside_vcs(self):
        ps = self._ps(vcs=uuid.uuid4())
        doc = _policy_set_json(ps, embed_policies=True)
        assert "policies" in doc["relationships"]
        assert "vcs-connection" in doc["relationships"]


class TestModuleWorkspaceLink:
    def test_workspace_relationship_and_attribute(self):
        ws_id = uuid.uuid4()
        link = SimpleNamespace(
            id=uuid.uuid4(),
            workspace_id=ws_id,
            workspace=SimpleNamespace(name="ws"),
            created_at=None,
            created_by="a@b.c",
        )
        doc = _link_to_jsonapi(link)
        assert doc["attributes"]["workspace-id"] == f"ws-{ws_id}"
        assert _rel_id(doc, "workspace") == f"ws-{ws_id}"
        assert doc["relationships"]["workspace"]["data"]["type"] == "workspaces"
