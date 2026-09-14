"""Module autodiscovery against real Postgres (#1584).

Integration tier deliberately: what needs proving is database behaviour a mock
cannot show — that deleting a rule leaves the modules it registered (the foreign
key goes NULL), that rule names are unique per connection, and that a module
registered by someone else between a scan's preview and its insert is skipped
by the savepoint while the rest of the scan still commits.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from terrapod.db.models import ModuleAutodiscoveryRule, RegistryModule, VCSConnection
from terrapod.db.session import get_db_session
from terrapod.services import module_autodiscovery_service as svc

pytestmark = pytest.mark.integration

PATHS = ["main.tf", "modules/a/main.tf", "modules/b/main.tf"]


def _repo(tag: str) -> str:
    return f"https://github.com/org/terraform-azurerm-mg{tag}"


async def _seed_rule(tag: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with get_db_session() as db:
        conn = VCSConnection(provider="github", name=f"conn-{tag}", server_url="", token="x")
        db.add(conn)
        await db.flush()
        rule = ModuleAutodiscoveryRule(
            vcs_connection_id=conn.id, repo_url=_repo(tag), pattern="**/*.tf", name=f"rule-{tag}"
        )
        db.add(rule)
        await db.commit()
        return conn.id, rule.id


async def _modules(tag: str) -> list[RegistryModule]:
    async with get_db_session() as db:
        rows = await db.execute(
            select(RegistryModule).where(RegistryModule.vcs_repo_url == _repo(tag))
        )
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_a_scan_registers_and_deleting_the_rule_keeps_the_modules(app):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)

    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        result = await svc.register_candidates(db, rule, PATHS)
        await db.commit()
    assert len(result.created) == 3

    async with get_db_session() as db:
        await db.delete(await db.get(ModuleAutodiscoveryRule, rule_id))
        await db.commit()

    modules = await _modules(tag)
    assert {m.subdirectory for m in modules} == {"", "modules/a", "modules/b"}
    assert {m.name for m in modules} == {f"mg{tag}", f"mg{tag}-a", f"mg{tag}-b"}
    assert all(m.module_autodiscovery_rule_id is None for m in modules)


@pytest.mark.asyncio
async def test_scanning_twice_registers_nothing_new(app):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)
    for expected in (3, 0):
        async with get_db_session() as db:
            rule = await db.get(ModuleAutodiscoveryRule, rule_id)
            result = await svc.register_candidates(db, rule, PATHS)
            await db.commit()
        assert len(result.created) == expected
    assert len(await _modules(tag)) == 3


@pytest.mark.asyncio
async def test_rule_names_are_unique_per_connection(app):
    tag = uuid.uuid4().hex[:8]
    conn_id, _ = await _seed_rule(tag)
    with pytest.raises(IntegrityError):
        async with get_db_session() as db:
            db.add(
                ModuleAutodiscoveryRule(
                    vcs_connection_id=conn_id, repo_url=_repo(tag), pattern="**", name=f"rule-{tag}"
                )
            )
            await db.commit()


@pytest.mark.asyncio
async def test_a_module_registered_concurrently_is_skipped_and_the_rest_commit(app):
    tag = uuid.uuid4().hex[:8]
    conn_id, rule_id = await _seed_rule(tag)

    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        stale = await svc.preview(db, rule, PATHS)

        # Someone else registers modules/a from the same repository, under another
        # name, after the preview was taken.
        async with get_db_session() as other:
            other.add(
                RegistryModule(
                    namespace="default",
                    name=f"other{tag}",
                    provider="azurerm",
                    source="vcs",
                    vcs_connection_id=conn_id,
                    vcs_repo_url=_repo(tag),
                    subdirectory="modules/a",
                )
            )
            await other.commit()

        # The scan works from the stale preview, so modules/a hits the partial
        # unique index on (vcs_repo_url, subdirectory).
        with patch.object(svc, "preview", return_value=stale):
            result = await svc.register_candidates(db, rule, PATHS)
        await db.commit()

    assert ("modules/a", "already-registered") in result.skipped
    assert {m.subdirectory for m in result.created} == {"", "modules/b"}
    by_sub = {m.subdirectory: m.name for m in await _modules(tag)}
    assert by_sub == {"": f"mg{tag}", "modules/a": f"other{tag}", "modules/b": f"mg{tag}-b"}
