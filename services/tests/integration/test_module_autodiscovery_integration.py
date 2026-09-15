"""Module autodiscovery against real Postgres (#1584).

Integration tier deliberately: what needs proving is database behaviour a mock
cannot show — that deleting a rule leaves the modules it registered (the foreign
key goes NULL), that rule names are unique per connection, that a module
registered by someone else between a scan's preview and its insert is skipped
by the savepoint while the rest of the scan still commits, that polling
baselines first and then registers only new directories (with
`seen_subdirectories` round-tripping through JSONB), and that one rule's
database error rolls back only that rule's work (#1633).
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from terrapod.db.models import ModuleAutodiscoveryRule, RegistryModule, VCSConnection
from terrapod.db.session import get_db_session
from terrapod.services import module_autodiscovery_service as svc

pytestmark = pytest.mark.integration

PATHS = ["main.tf", "modules/a/main.tf", "modules/b/main.tf"]
_GH = "terrapod.services.github_service"


def _repo(tag: str) -> str:
    return f"https://github.com/org/terraform-azurerm-mg{tag}"


async def _seed_rule(tag: str, **rule_fields) -> tuple[uuid.UUID, uuid.UUID]:
    async with get_db_session() as db:
        # (provider, github_installation_id) is unique, so a test seeding two
        # connections needs distinct installation ids; derive one from the tag.
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
        rule = ModuleAutodiscoveryRule(
            vcs_connection_id=conn.id,
            repo_url=_repo(tag),
            pattern="**/*.tf",
            name=f"rule-{tag}",
            **rule_fields,
        )
        db.add(rule)
        await db.commit()
        return conn.id, rule.id


@contextmanager
def _vcs(heads: dict[str, tuple[str, list[str]]]):
    """Serve each tag's repository at a head sha with a file tree.

    Every other repository — rules left behind by other tests in this database
    — fails to list, so polling those rules changes nothing.
    """
    by_repo = {_repo(tag).rsplit("/", 1)[-1]: v for tag, v in heads.items()}

    async def sha(conn, owner, repo, branch):
        return by_repo[repo][0] if repo in by_repo else None

    async def tree(conn, owner, repo, ref):
        if repo not in by_repo:
            raise RuntimeError("not this test's repository")
        return by_repo[repo][1]

    with (
        patch(f"{_GH}.get_repo_default_branch", new=AsyncMock(return_value="main")),
        patch(f"{_GH}.get_repo_branch_sha", new=sha),
        patch(f"{_GH}.list_repo_tree", new=tree),
    ):
        yield


async def _poll() -> int:
    async with get_db_session() as db:
        registered = await svc.poll_rules(db)
        await db.commit()
    return registered


async def _rule(rule_id: uuid.UUID) -> ModuleAutodiscoveryRule:
    async with get_db_session() as db:
        return await db.get(ModuleAutodiscoveryRule, rule_id)


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


@pytest.mark.asyncio
async def test_polling_baselines_then_registers_only_new_directories(app):
    tag = uuid.uuid4().hex[:8]
    _, rule_id = await _seed_rule(tag)

    # First poll: a baseline. Nothing is registered, and what is there is seen.
    with _vcs({tag: ("s1", PATHS)}):
        assert await _poll() == 0
    assert await _modules(tag) == []
    rule = await _rule(rule_id)
    assert rule.seen_subdirectories == ["", "modules/a", "modules/b"]
    assert rule.last_scanned_sha == "s1" and rule.first_scan_at is not None

    # The operator ticks only modules/a; the root and modules/b stay unticked.
    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        result = await svc.register_candidates(db, rule, PATHS, only=["modules/a"])
        svc.record_scan(rule, PATHS, "s1")
        await db.commit()
    assert [m.subdirectory for m in result.created] == ["modules/a"]

    # The branch moves and modules/c appears: only it is registered.
    with _vcs({tag: ("s2", [*PATHS, "modules/c/main.tf"])}):
        assert await _poll() == 1
    modules = await _modules(tag)
    assert {m.subdirectory for m in modules} == {"modules/a", "modules/c"}
    assert all(m.module_autodiscovery_rule_id == rule_id for m in modules)
    rule = await _rule(rule_id)
    assert rule.seen_subdirectories == ["", "modules/a", "modules/b", "modules/c"]
    assert rule.last_scanned_sha == "s2"

    # An unmoved branch is not walked again, and registers nothing more.
    with _vcs({tag: ("s2", [*PATHS, "modules/c/main.tf", "modules/d/main.tf"])}):
        assert await _poll() == 0
    assert len(await _modules(tag)) == 2


@pytest.mark.asyncio
async def test_a_database_error_in_one_rule_does_not_roll_back_another(app):
    # Both rules have a baseline and find a new directory. The broken rule's
    # head sha is too long for its column, so recording its scan fails at the
    # flush — after its module was inserted. Its savepoint rolls back its work,
    # and the other rule's registration still commits.
    baseline = {
        "first_scan_at": datetime.now(UTC),
        "last_scanned_sha": "s1",
        "seen_subdirectories": ["", "modules/a", "modules/b"],
    }
    broken_tag, good_tag = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
    _, broken_id = await _seed_rule(broken_tag, **baseline)
    _, good_id = await _seed_rule(good_tag, **baseline)
    tree = [*PATHS, "modules/c/main.tf"]

    with _vcs({broken_tag: ("x" * 100, tree), good_tag: ("s2", tree)}):
        assert await _poll() == 1

    assert await _modules(broken_tag) == []
    broken = await _rule(broken_id)
    assert broken.last_scanned_sha == "s1"
    assert broken.seen_subdirectories == ["", "modules/a", "modules/b"]

    assert [m.subdirectory for m in await _modules(good_tag)] == ["modules/c"]
    good = await _rule(good_id)
    assert good.last_scanned_sha == "s2" and "modules/c" in good.seen_subdirectories
