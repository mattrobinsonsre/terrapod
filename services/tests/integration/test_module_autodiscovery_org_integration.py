"""Org-wide module autodiscovery against real Postgres (#1620).

For what a fake database cannot show: the per-repository rows and the modules
they register actually committing; a database error in one repository's
savepoint undoing only that repository (and the row being recorded as failed
afterwards, on a session that has just rolled a savepoint back); the SQL-side
URL normalisation that stops a hand-registered module being registered twice;
a rename keeping one row per repository under the partial unique index; and
`/repositories` paging and filtering in the database.
"""

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    RegistryModule,
    VCSConnection,
)
from terrapod.db.session import get_db_session
from terrapod.services import module_autodiscovery_service as svc
from tests.integration.conftest import AUTH, admin_user, set_auth
from tests.services.test_module_autodiscovery_org_poll import FUTURE, TREE, FakeGitHub, _serve

pytestmark = pytest.mark.integration


async def _seed(**rule_fields) -> uuid.UUID:
    async with get_db_session() as db:
        conn = VCSConnection(
            provider="github",
            name="gh",
            server_url="",
            token="x",
            status="active",
            github_installation_id=4242,
            github_account_login="org",
        )
        db.add(conn)
        await db.flush()
        fields = {
            "vcs_connection_id": conn.id,
            "repo_url": "org",
            "target_kind": "namespace",
            "target_id": "1",
            "pattern": "**/*.tf",
            "name": "org-rule",
        }
        fields.update(rule_fields)
        rule = ModuleAutodiscoveryRule(**fields)
        db.add(rule)
        await db.commit()
        return rule.id


async def _poll(gh: FakeGitHub) -> int:
    with _serve(gh):
        async with get_db_session() as db:
            registered = await svc.poll_rules(db)
            await db.commit()
    return registered


async def _rows(rule_id) -> dict[str, ModuleAutodiscoveryRepository]:
    async with get_db_session() as db:
        rows = await db.execute(
            select(ModuleAutodiscoveryRepository).where(
                ModuleAutodiscoveryRepository.rule_id == rule_id
            )
        )
        return {r.repo_path: r for r in rows.scalars().all()}


async def _modules() -> list[RegistryModule]:
    async with get_db_session() as db:
        return list((await db.execute(select(RegistryModule))).scalars().all())


@pytest.mark.asyncio
async def test_a_baseline_then_a_new_repository_registers_everything(app):
    gh = FakeGitHub()
    gh.add("org/terraform-aws-a", 1)
    rule_id = await _seed()
    assert await _poll(gh) == 0
    rows = await _rows(rule_id)
    assert rows["org/terraform-aws-a"].origin == "baseline"
    assert rows["org/terraform-aws-a"].seen_subdirectories == ["", "modules/a"]
    assert rows["org/terraform-aws-a"].candidates[1]["name"] == "a-a"

    gh.add("org/terraform-aws-new", 2, created=FUTURE)
    assert await _poll(gh) == 2
    modules = await _modules()
    assert {(m.name, m.subdirectory) for m in modules} == {("new", ""), ("new-a", "modules/a")}
    assert all(m.vcs_repo_url == "https://github.com/org/terraform-aws-new" for m in modules)
    assert all(m.module_autodiscovery_rule_id == rule_id for m in modules)
    assert (await _rows(rule_id))["org/terraform-aws-new"].origin == "new"
    async with get_db_session() as db:
        rule = await db.get(ModuleAutodiscoveryRule, rule_id)
        assert rule.first_scan_at is not None and rule.last_enumerated_at is not None


@pytest.mark.asyncio
async def test_a_database_error_in_one_repository_undoes_only_that_repository(app):
    gh = FakeGitHub()
    rule_id = await _seed()
    assert await _poll(gh) == 0  # the baseline, of nothing
    # Both are new; the broken one's head is too long for its column, so its
    # savepoint fails at the flush — after its modules were added.
    gh.add("org/terraform-aws-broken", 1, created=FUTURE, head="x" * 100)
    gh.add("org/terraform-aws-good", 2, created=FUTURE)
    assert await _poll(gh) == 2
    modules = await _modules()
    assert {m.vcs_repo_url for m in modules} == {"https://github.com/org/terraform-aws-good"}
    rows = await _rows(rule_id)
    broken = rows["org/terraform-aws-broken"]
    assert broken.status == "error" and broken.failure_count == 1
    assert "could not record the scan" in broken.last_error
    assert broken.last_scanned_sha == "" and broken.seen_subdirectories == []
    assert rows["org/terraform-aws-good"].last_scanned_sha == "s1"


@pytest.mark.asyncio
async def test_a_hand_registered_module_at_a_differently_written_url_is_not_duplicated(app):
    gh = FakeGitHub()
    rule_id = await _seed()
    await _poll(gh)
    async with get_db_session() as db:
        conn_id = (await db.get(ModuleAutodiscoveryRule, rule_id)).vcs_connection_id
        db.add(
            RegistryModule(
                namespace="default",
                name="handmade",
                provider="aws",
                source="vcs",
                vcs_connection_id=conn_id,
                vcs_repo_url="https://GitHub.com/Org/Terraform-AWS-new.git/",
                subdirectory="",
            )
        )
        await db.commit()
    gh.add("org/terraform-aws-new", 2, created=FUTURE)
    assert await _poll(gh) == 1
    by_sub = {m.subdirectory: m.name for m in await _modules()}
    assert by_sub == {"": "handmade", "modules/a": "new-a"}
    row = (await _rows(rule_id))["org/terraform-aws-new"]
    assert row.last_skips == [{"subdirectory": "", "reason": "already-registered"}]


@pytest.mark.asyncio
async def test_a_rename_keeps_one_row_per_repository(app):
    gh = FakeGitHub()
    gh.add("org/terraform-aws-a", 1)
    rule_id = await _seed()
    await _poll(gh)
    gh.rename("org/terraform-aws-a", "org/terraform-aws-z")
    gh.push("org/terraform-aws-z", head="s2", pushed="p2", tree=[*TREE, "modules/b/main.tf"])
    assert await _poll(gh) == 1
    rows = await _rows(rule_id)
    assert list(rows) == ["org/terraform-aws-z"]
    assert rows["org/terraform-aws-z"].previous_paths == [
        {"path": "org/terraform-aws-a", "url": "https://github.com/org/terraform-aws-a"}
    ]
    (module,) = await _modules()
    assert module.vcs_repo_url == "https://github.com/org/terraform-aws-z"


@pytest.mark.asyncio
async def test_the_repositories_endpoint_pages_and_filters_in_the_database(app, client):
    gh = FakeGitHub()
    for i, name in enumerate(("a", "b", "c")):
        gh.add(f"org/terraform-aws-{name}", i + 1, archived=name == "c")
    rule_id = await _seed()
    await _poll(gh)

    set_auth(app, admin_user())
    base = f"/api/terrapod/v1/module-autodiscovery-rules/modrule-{rule_id}/repositories"
    resp = await client.get(base, params={"page[size]": "2"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d["attributes"]["repository"] for d in body["data"]] == [
        "org/terraform-aws-a",
        "org/terraform-aws-b",
    ]
    assert body["meta"]["pagination"] == {
        "current-page": 1,
        "page-size": 2,
        "total-count": 3,
        "total-pages": 2,
    }
    resp = await client.get(base, params={"filter[status]": "archived"}, headers=AUTH)
    assert [d["attributes"]["repository"] for d in resp.json()["data"]] == ["org/terraform-aws-c"]

    # The stored preview is served from these rows, a page at a time.
    resp = await client.get(
        f"/api/terrapod/v1/module-autodiscovery-rules/modrule-{rule_id}/preview", headers=AUTH
    )
    attrs = resp.json()["data"]["attributes"]
    assert [r["repository"] for r in attrs["repositories"]] == [
        "org/terraform-aws-a",
        "org/terraform-aws-b",
    ]
