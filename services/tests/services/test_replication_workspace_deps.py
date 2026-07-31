"""Replication of what a workspace points at (#1134).

Workspaces cannot replicate until these exist on the peer. Every one of
`vcs_connection_id`, `autodiscovery_rule_id` and `catalog_item_id` is a real
foreign key, and a *nullable* foreign key is still an enforced one — so none of
it can be deferred by nulling the column on the follower. Nor should it be:
`workspace_rbac_service` clamps permissions on a `catalog_item_id`-bearing
workspace, so dropping that id would **widen** access at the moment of a
failover.

What each one costs when it is missing is different, and worth stating, because
none of the three announces itself:

- **registry modules** — catalog items and module-workspace links lose their
  foreign-key target, so neither can replicate at all.
- **catalog items** — self-service provisioning is gone, and the RBAC clamp has
  nothing to key off.
- **autodiscovery rules** — new directories stop getting workspaces, and the
  lifecycle reconciler loses the `on_directory_delete` setting that decides what
  a *removed* directory means. The safe default it falls back to is not
  necessarily the one the operator chose.

`TestDependencyOrderIsReal` derives the ordering requirement from the schema
rather than trusting the comments in the registry.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect

from terrapod.db.models import AutodiscoveryRule, CatalogItem, ProviderTemplate, RegistryModule
from terrapod.services import replication, replication_registry

MODULES = replication_registry.REGISTRY_MODULES
TEMPLATES = replication_registry.PROVIDER_TEMPLATES
ITEMS = replication_registry.CATALOG_ITEMS
RULES = replication_registry.AUTODISCOVERY_RULES

CONN_ID = "11111111-1111-1111-1111-111111111111"
MODULE_ID = "22222222-2222-2222-2222-222222222222"
OTHER_ID = "33333333-3333-3333-3333-333333333333"


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _keys_db(keys):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = keys
    db.execute.return_value = result
    return db


def _stamps():
    now = datetime.now(UTC)
    return {"created_at": now, "updated_at": now}


def _module(module_id=MODULE_ID, **kw):
    base = {
        "id": module_id,
        "namespace": "default",
        "name": "vpc",
        "provider": "aws",
        "status": "active",
        "source": "vcs",
        "vcs_connection_id": CONN_ID,
        "vcs_repo_url": "https://github.com/example/terraform-aws-vpc",
        "vcs_tag_pattern": "v*",
        "vcs_last_tag": "v1.4.0",
        **_stamps(),
    }
    base.update(kw)
    return RegistryModule(**base)


def _template(template_id=OTHER_ID, **kw):
    base = {
        "id": template_id,
        "name": "aws-prod",
        "provider_type": "aws",
        "body": 'provider "aws" {\n  region = var.region\n}\n',
        "parameters": [{"name": "region", "required": True}],
        **_stamps(),
    }
    base.update(kw)
    return ProviderTemplate(**base)


def _item(item_id=OTHER_ID, **kw):
    base = {
        "id": item_id,
        "module_id": MODULE_ID,
        "name": "vpc",
        "display_name": "Virtual network",
        "enabled": True,
        "provider_template_ids": [OTHER_ID],
        "variable_options": [],
        **_stamps(),
    }
    base.update(kw)
    return CatalogItem(**base)


def _rule(rule_id=OTHER_ID, **kw):
    base = {
        "id": rule_id,
        "vcs_connection_id": CONN_ID,
        "repo_url": "https://github.com/example/infra",
        "pattern": "envs/*/",
        "ignore_patterns": ["envs/scratch/"],
        "name": "envs",
        "enabled": True,
        "execution_mode": "agent",
        "on_directory_delete": "flag",
        **_stamps(),
    }
    base.update(kw)
    return AutodiscoveryRule(**base)


class TestRegistryModules:
    @pytest.mark.replication_matrix("registry_modules", "backfill-from-empty")
    async def test_backfill_carries_the_identity_and_its_vcs_source(self):
        db = _rows_db([_module()])

        page = await replication.read_backfill(db, MODULES)

        assert (page[0]["namespace"], page[0]["name"], page[0]["provider"]) == (
            "default",
            "vpc",
            "aws",
        )
        assert page[0]["vcs_connection_id"] == CONN_ID

    @pytest.mark.replication_matrix("registry_modules", "delta-apply")
    async def test_the_tag_cursor_reaches_the_peer(self):
        """`vcs_last_tag` is the auto-publish cursor. A promoted node that has
        not seen it re-publishes every tag it finds, which fires a run on every
        linked workspace."""
        db = AsyncMock()
        existing = _module(vcs_last_tag="v1.3.0")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, MODULES, {"id": MODULE_ID, "vcs_last_tag": "v1.4.0", "name": "vpc"}
        )

        assert existing.vcs_last_tag == "v1.4.0"

    @pytest.mark.replication_matrix("registry_modules", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _module()
        db.scalar.return_value = existing
        payload = replication.serialize_row(MODULES, existing)

        await replication.apply_upsert(db, MODULES, payload)
        await replication.apply_upsert(db, MODULES, payload)

        assert existing.name == "vpc"
        assert existing.vcs_last_tag == "v1.4.0"

    @pytest.mark.replication_matrix("registry_modules", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, MODULES, MODULE_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("registry_modules", "backfill-converges-deletion")
    async def test_a_deleted_module_does_not_survive_a_backfill(self):
        db = _keys_db([(MODULE_ID,), (OTHER_ID,)])

        removed = await replication.reconcile_deletions(db, MODULES, {MODULE_ID})

        assert removed == [OTHER_ID]

    def test_module_versions_are_in_scope_now_that_their_tarballs_copy(self):
        """This assertion used to be its inverse, and the reasoning was sound at
        the time: a version row points at a tarball in object storage, and
        shipping the row without the content makes `terraform init` resolve a
        version and then 404 mid-run. Withholding it made the registry report no
        matching version, which was at least true.

        #1114 copies the tarballs, which reverses the argument — the content now
        arrives and the row does not, so the registry reports no version for a
        module whose bytes are sitting in the store. Same lie, other direction,
        and this one wastes a working artifact (#1175)."""
        assert "registry_module_versions" in replication.registered()


class TestProviderTemplates:
    @pytest.mark.replication_matrix("provider_templates", "backfill-from-empty")
    async def test_backfill_carries_the_body_and_its_parameters(self):
        db = _rows_db([_template()])

        page = await replication.read_backfill(db, TEMPLATES)

        assert 'provider "aws"' in page[0]["body"]
        assert page[0]["parameters"] == [{"name": "region", "required": True}]

    @pytest.mark.replication_matrix("provider_templates", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _template(body="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, TEMPLATES, {"id": OTHER_ID, "body": "new"})

        assert existing.body == "new"

    @pytest.mark.replication_matrix("provider_templates", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _template()
        db.scalar.return_value = existing
        payload = replication.serialize_row(TEMPLATES, existing)

        await replication.apply_upsert(db, TEMPLATES, payload)
        await replication.apply_upsert(db, TEMPLATES, payload)

        assert existing.name == "aws-prod"
        assert existing.parameters == [{"name": "region", "required": True}]

    @pytest.mark.replication_matrix("provider_templates", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, TEMPLATES, OTHER_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("provider_templates", "backfill-converges-deletion")
    async def test_a_deleted_template_does_not_survive_a_backfill(self):
        db = _keys_db([(OTHER_ID,), (MODULE_ID,)])

        removed = await replication.reconcile_deletions(db, TEMPLATES, {OTHER_ID})

        assert removed == [MODULE_ID]


class TestCatalogItems:
    @pytest.mark.replication_matrix("catalog_items", "backfill-from-empty")
    async def test_backfill_carries_the_module_link_and_the_form_overlay(self):
        db = _rows_db([_item(variable_options=[{"name": "cidr", "options": ["10.0.0.0/16"]}])])

        page = await replication.read_backfill(db, ITEMS)

        assert page[0]["module_id"] == MODULE_ID
        assert page[0]["variable_options"][0]["name"] == "cidr"
        assert page[0]["provider_template_ids"] == [OTHER_ID]

    @pytest.mark.replication_matrix("catalog_items", "delta-apply")
    async def test_disabling_an_item_reaches_the_peer(self):
        """Disabling is how an admin withdraws an offering. A promoted node still
        offering it would provision something the operator retired."""
        db = AsyncMock()
        existing = _item(enabled=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, ITEMS, {"id": OTHER_ID, "enabled": False})

        assert existing.enabled is False

    @pytest.mark.replication_matrix("catalog_items", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _item()
        db.scalar.return_value = existing
        payload = replication.serialize_row(ITEMS, existing)

        await replication.apply_upsert(db, ITEMS, payload)
        await replication.apply_upsert(db, ITEMS, payload)

        assert existing.name == "vpc"
        assert existing.enabled is True

    @pytest.mark.replication_matrix("catalog_items", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, ITEMS, OTHER_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("catalog_items", "backfill-converges-deletion")
    async def test_a_deleted_item_does_not_survive_a_backfill(self):
        db = _keys_db([(OTHER_ID,), (MODULE_ID,)])

        removed = await replication.reconcile_deletions(db, ITEMS, {OTHER_ID})

        assert removed == [MODULE_ID]

    async def test_a_null_pool_allow_list_stays_null(self):
        """NULL and `[]` mean opposite things here: NULL is 'any pool the
        consumer can use', empty is 'none'. Collapsing one into the other either
        widens provisioning or breaks it."""
        db = AsyncMock()
        existing = _item()
        db.scalar.return_value = existing
        payload = replication.serialize_row(ITEMS, _item(allowed_agent_pool_ids=None))

        await replication.apply_upsert(db, ITEMS, payload)

        assert existing.allowed_agent_pool_ids is None


class TestAutodiscoveryRules:
    @pytest.mark.replication_matrix("autodiscovery_rules", "backfill-from-empty")
    async def test_backfill_carries_the_match_and_the_template(self):
        db = _rows_db([_rule()])

        page = await replication.read_backfill(db, RULES)

        assert page[0]["pattern"] == "envs/*/"
        assert page[0]["ignore_patterns"] == ["envs/scratch/"]
        assert page[0]["vcs_connection_id"] == CONN_ID

    @pytest.mark.replication_matrix("autodiscovery_rules", "delta-apply")
    async def test_the_deletion_policy_reaches_the_peer(self):
        """`on_directory_delete` decides whether a removed directory flags a
        workspace for a human or queues a real destroy. A promoted node holding
        the wrong value either strands workspaces or destroys infrastructure the
        operator expected to be flagged — so this is the single most
        consequential field in the class."""
        db = AsyncMock()
        existing = _rule(on_directory_delete="destroy")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, RULES, {"id": OTHER_ID, "on_directory_delete": "flag"})

        assert existing.on_directory_delete == "flag"

    @pytest.mark.replication_matrix("autodiscovery_rules", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _rule()
        db.scalar.return_value = existing
        payload = replication.serialize_row(RULES, existing)

        await replication.apply_upsert(db, RULES, payload)
        await replication.apply_upsert(db, RULES, payload)

        assert existing.pattern == "envs/*/"
        assert existing.on_directory_delete == "flag"
        assert existing.enabled is True

    @pytest.mark.replication_matrix("autodiscovery_rules", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, RULES, OTHER_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("autodiscovery_rules", "backfill-converges-deletion")
    async def test_a_deleted_rule_does_not_survive_a_backfill(self):
        """A rule that comes back to life resumes creating workspaces from a
        pattern somebody deliberately removed."""
        db = _keys_db([(OTHER_ID,), (MODULE_ID,)])

        removed = await replication.reconcile_deletions(db, RULES, {OTHER_ID})

        assert removed == [MODULE_ID]

    async def test_a_disabled_rule_stays_disabled(self):
        db = AsyncMock()
        existing = _rule(enabled=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, RULES, {"id": OTHER_ID, "enabled": False})

        assert existing.enabled is False


class TestDependencyOrderIsReal:
    """Backfill walks the registry in order and inserts as it goes, so a class
    registered before something it references fails on a foreign key — on a
    promoted node's first backfill, which is the worst place to discover it.

    This derives the requirement from the schema rather than trusting the
    comments in the registry, so a model that gains a foreign key later starts
    failing here instead of failing at a failover.
    """

    def _position(self, name: str) -> int:
        return list(replication.registered()).index(name)

    def _table_of(self, name: str):
        return sa_inspect(replication.get(name).model).local_table

    def test_every_replicated_fk_target_is_registered_earlier(self):
        registered = replication.registered()
        by_table = {self._table_of(name).name: name for name in registered}
        problems = []

        for name in registered:
            for fk in self._table_of(name).foreign_keys:
                target = fk.column.table.name
                if target == self._table_of(name).name:
                    continue  # self-reference, nothing to order against
                if target not in by_table:
                    continue  # not replicated yet — the class tolerates its absence
                if self._position(by_table[target]) > self._position(name):
                    problems.append(f"{name} is registered before {by_table[target]}")

        assert not problems, (
            "Registration order violates a foreign key. Backfill inserts in this "
            "order, so this fails on a promoted node's first backfill:\n  " + "\n  ".join(problems)
        )

    def test_the_check_can_actually_fail(self):
        """A gate that cannot fail is not a gate: prove the ordering check would
        catch a reversal rather than silently passing on an empty loop."""
        found = [
            (name, fk.column.table.name)
            for name in replication.registered()
            for fk in self._table_of(name).foreign_keys
            if fk.column.table.name != self._table_of(name).name
        ]

        assert found, "no foreign keys were inspected — the ordering check is inert"

    def test_the_chain_is_in_the_expected_order(self):
        """The specific order this slice depends on, stated once so a reader does
        not have to derive it."""
        order = list(replication.registered())

        for earlier, later in (
            ("vcs_connections", "registry_modules"),
            ("registry_modules", "catalog_items"),
            ("provider_templates", "catalog_items"),
            ("vcs_connections", "autodiscovery_rules"),
            ("agent_pools", "autodiscovery_rules"),
        ):
            assert order.index(earlier) < order.index(later), f"{earlier} must precede {later}"
