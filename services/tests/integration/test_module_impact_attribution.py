"""Integration: a run with no `module_impact_module_id` is still cancelled (#2425 §1.3).

Real Postgres, because what is under test is a `WHERE` clause over a JSONB key
and both halves are invisible to a mocked session -- `->` returning SQL NULL for
a missing key, and the `OR` branch matching a row whose id column is NULL.

`alembic/versions/a9a7825cce45_cv_archive_provenance.py` adds
`runs.module_impact_module_id` nullable with **no backfill, deliberately**: an
override key records a *coordinate*, and after a module is deleted the same
coordinate can name a different module, so deriving the id from it would
attribute a historical run to whoever holds that coordinate now. Its own comment
says exactly that.

The cost of leaving it NULL is that five predicates stopped matching those rows,
and three of them are not posting paths -- they cancel a run or suppress a
duplicate, scoped by `workspace_id` and `vcs_pull_request_number` as well. So a
run that was in flight across that one upgrade was never cancelled when its PR
closed or a new commit landed, and never suppressed as a duplicate, which queues
a second plan for every linked workspace on the next poll.

`_attributed_to` is the narrow fallback for those three. The two paths that POST
-- a commit status, and the PR comment -- still require the id, because there a
wrong answer routes one module's run details into another's repository. That
asymmetry is the whole design and each site carries a comment saying so; this
file asserts both halves, so making them "consistent" fails here.
"""

import uuid as _uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import (
    AgentPool,
    ConfigurationVersion,
    ModuleWorkspaceLink,
    RegistryModule,
    Run,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.services import module_impact_service, pool_set

pytestmark = pytest.mark.integration


async def _seed(*, with_id: bool) -> tuple[_uuid.UUID, _uuid.UUID, str]:
    """A module, a linked workspace, and one in-flight module-test run.

    `with_id` chooses which era the run comes from: a run created today carries
    the module id, and one created before the column existed carries only the
    override coordinate.
    """
    suffix = _uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        module = RegistryModule(
            namespace="acme",
            name=f"net{suffix}",
            provider="aws",
            vcs_repo_url="https://example.invalid/acme/net",
        )
        db.add(module)
        await db.flush()

        pool = AgentPool(name=f"mia-{suffix}")
        db.add(pool)
        await db.flush()

        ws = Workspace(name=f"mia-consumer-{suffix}", execution_mode="agent")
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()

        db.add(
            ModuleWorkspaceLink(
                module_id=module.id, workspace_id=ws.id, created_by="itest@example.com"
            )
        )

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="module-test")
        db.add(cv)
        await db.flush()

        coord = f"{module.namespace}/{module.name}/{module.provider}"
        run = Run(
            workspace_id=ws.id,
            configuration_version_id=cv.id,
            status="planning",
            source="module-test",
            plan_only=True,
            message="module impact",
            created_by=module_impact_service.run_service.MODULE_IMPACT_POLLER_CREATED_BY,
            vcs_pull_request_number=4242,
            vcs_commit_sha="c" * 40,
        )
        # Written through the ORM rather than an INSERT: `runs` carries ~80
        # columns, so a hand-written statement breaks on the next NOT NULL
        # anyone adds, with a driver error naming a column unrelated to the test.
        run.module_overrides = {coord: f"module-overrides/{suffix}.tar.gz"}
        run.module_impact_module_id = module.id if with_id else None
        db.add(run)
        await db.flush()
        return module.id, run.id, coord


async def _status_of(run_id: _uuid.UUID) -> str:
    async with get_db_session() as db:
        return (await db.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


@pytest.mark.parametrize(
    ("with_id", "label"),
    [(True, "a run created today"), (False, "a run predating the column")],
)
async def test_a_closed_pr_cancels_the_run_whether_or_not_the_id_is_set(with_id, label):
    """Both eras, in one parametrize, because the property is the RELATIONSHIP.

    A fixture carrying only the modern row passes whichever way `_attributed_to`
    is written -- the id branch alone satisfies it -- so the pre-upgrade row is
    the half that makes this a gate rather than a description.
    """
    module_id, run_id, _ = await _seed(with_id=with_id)
    async with get_db_session() as db:
        module = (
            await db.execute(select(RegistryModule).where(RegistryModule.id == module_id))
        ).scalar_one()
        await db.refresh(module, ["workspace_links"])
        await module_impact_service._cancel_stale_module_runs(db, module, 4242)
        await db.commit()

    assert await _status_of(run_id) == "canceled", f"{label} was left in flight"


async def test_the_fallback_does_not_reach_another_modules_run():
    """The coordinate is the key, so it must not match a different module's.

    **Both modules are linked to the SAME workspace**, deliberately. Two modules
    in two workspaces proves nothing: `_cancel_stale_module_runs` iterates one
    module's own links, so the workspace scoping would do all the work and the
    coordinate test could be deleted with this still passing. That is exactly
    what the first version of this test did -- the mutation dropping the
    coordinate clause left it green.

    One workspace consuming two modules is also the realistic shape: that is what
    a module registry is for.
    """
    suffix = _uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        mods = []
        for n in ("alpha", "beta"):
            m = RegistryModule(
                namespace="acme",
                name=f"{n}{suffix}",
                provider="aws",
                vcs_repo_url=f"https://example.invalid/acme/{n}",
            )
            db.add(m)
            mods.append(m)
        pool = AgentPool(name=f"mia2-{suffix}")
        db.add(pool)
        await db.flush()

        ws = Workspace(name=f"mia2-consumer-{suffix}", execution_mode="agent")
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()
        for m in mods:
            db.add(
                ModuleWorkspaceLink(
                    module_id=m.id, workspace_id=ws.id, created_by="itest@example.com"
                )
            )

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="module-test")
        db.add(cv)
        await db.flush()

        # One NULL-id run, whose overrides name ALPHA only.
        run = Run(
            workspace_id=ws.id,
            configuration_version_id=cv.id,
            status="planning",
            source="module-test",
            plan_only=True,
            message="module impact",
            created_by=module_impact_service.run_service.MODULE_IMPACT_POLLER_CREATED_BY,
            vcs_pull_request_number=4242,
            vcs_commit_sha="c" * 40,
        )
        run.module_overrides = {
            f"{mods[0].namespace}/{mods[0].name}/{mods[0].provider}": "ov/alpha.tar.gz"
        }
        run.module_impact_module_id = None
        db.add(run)
        await db.flush()
        run_id, beta_id, alpha_id = run.id, mods[1].id, mods[0].id

    async with get_db_session() as db:
        beta = (
            await db.execute(select(RegistryModule).where(RegistryModule.id == beta_id))
        ).scalar_one()
        await db.refresh(beta, ["workspace_links"])
        await module_impact_service._cancel_stale_module_runs(db, beta, 4242)
        await db.commit()
    assert await _status_of(run_id) == "planning", "beta cancelled alpha's run"

    # And the positive control, in the same workspace: alpha DOES reach it, so
    # the assertion above is about the coordinate and not about a run that
    # nothing could have cancelled.
    async with get_db_session() as db:
        alpha = (
            await db.execute(select(RegistryModule).where(RegistryModule.id == alpha_id))
        ).scalar_one()
        await db.refresh(alpha, ["workspace_links"])
        await module_impact_service._cancel_stale_module_runs(db, alpha, 4242)
        await db.commit()
    assert await _status_of(run_id) == "canceled", "alpha did not reach its own run"


async def test_a_run_with_no_overrides_at_all_is_not_matched():
    """`module_overrides` is nullable too, and `NULL -> 'k'` is NULL.

    So the fallback must not degenerate into "any NULL-id module-test run in this
    workspace and PR", which is what a presence test written the other way round
    would do.
    """
    module_id, run_id, _ = await _seed(with_id=False)
    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        run.module_overrides = None
        await db.commit()
    async with get_db_session() as db:
        module = (
            await db.execute(select(RegistryModule).where(RegistryModule.id == module_id))
        ).scalar_one()
        await db.refresh(module, ["workspace_links"])
        await module_impact_service._cancel_stale_module_runs(db, module, 4242)
        await db.commit()

    assert await _status_of(run_id) == "planning", "a run with no overrides was matched"
