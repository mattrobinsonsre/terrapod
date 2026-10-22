"""A composite re-resolves its inventory at the hand-over (#2230).

The artifact is written at run CREATION -- before the plan, let alone the apply
-- and the runner fetches it rather than re-deriving it, deliberately. So
without this a composite configured the inventory as it stood before its own
apply, and an apply that CREATES hosts never configured them. That is the
provision-then-declare pattern the Ansible story is built on, so it was the
headline case rather than an edge.

#2021 froze the resolution at creation to keep the playbook and the hosts on one
commit, which still holds: this re-resolution reads the same configuration
version. What it picks up is the declared ROWS, which are not versioned with the
code and which the run's own apply deliberately changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers.runs import _apply_status
from terrapod.services import inventory_narrowing, inventory_resolve, run_service

pytestmark = pytest.mark.asyncio


def _run(*, composite: bool, apply_finished=None):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.status = "applying"
    run.configure_after_apply = composite
    run.is_configure = composite
    run.apply_started_at = datetime.now(UTC)
    run.apply_finished_at = apply_finished
    run.configure_started_at = None
    run.configure_finished_at = None
    run.error_message = ""
    run.job_name = "j"
    run.job_namespace = "n"
    return run


async def _complete(run, *, artifact=None):
    db = AsyncMock()
    db.get = AsyncMock(return_value=MagicMock())
    write = artifact if artifact is not None else AsyncMock(return_value=("key", {}))
    with (
        patch.object(inventory_resolve, "write_run_artifact", write),
        # The hand-over also narrows the configure now (#2229). Stubbed at the
        # boundary below the thing under test, so these tests stay about the
        # re-resolution; the narrowing has its own tests.
        patch.object(inventory_narrowing, "limit_for", AsyncMock(return_value="")),
        patch.object(run_service, "transition_run", AsyncMock(side_effect=_fake_transition)),
        patch.object(run_service, "_after_apply_landed", AsyncMock()),
        patch("terrapod.redis.client.delete_job_status", new_callable=AsyncMock),
    ):
        out = await run_service.complete_apply(db, run)
    return out, write


async def _fake_transition(db, run, target, **kw):
    """Only what the assertions need: the status, and the stamping rules that
    `_apply_status` reads (`transition_run` sets `configure_finished_at` when a
    run ends with `configure_started_at` already set)."""
    run.status = target
    if target in ("applied", "errored") and run.apply_started_at and not run.apply_finished_at:
        run.apply_finished_at = datetime.now(UTC)
    if target in ("applied", "errored", "canceled") and run.configure_started_at:
        run.configure_finished_at = run.configure_finished_at or datetime.now(UTC)
    return run


class TestTheCompositeReResolves:
    async def test_it_re_resolves_at_the_handover(self):
        run = _run(composite=True)
        _out, write = await _complete(run)
        write.assert_awaited_once()

    async def test_it_resolves_BEFORE_the_handover_is_visible(self):
        """`apply_finished_at` is exactly what the claim loop matches on, so the
        fresh bytes have to be in place before it is stamped. Asserted on the
        value the resolver SAW rather than on source order, because the ordering
        is the property and a positional check would pass on a reordering that
        kept the lines adjacent."""
        run = _run(composite=True)
        seen: list[object] = []

        async def _record(db, r, **kw):
            seen.append(r.apply_finished_at)
            return "key"

        await _complete(run, artifact=AsyncMock(side_effect=_record))
        assert seen == [None], (
            "the inventory was resolved after `apply_finished_at` was stamped, so a "
            "listener could claim the configure and fetch the PRE-apply artifact"
        )
        assert run.apply_finished_at is not None, "the hand-over never happened"


class TestNothingElseReResolves:
    async def test_a_plain_apply_does_not(self):
        run = _run(composite=False)
        out, write = await _complete(run)
        write.assert_not_awaited()
        assert out.status == "applied"

    async def test_a_second_pass_does_not(self):
        """The runner's POST and the reconciler both reach here, and the
        reconciler keeps reaching it until a listener claims the configure."""
        run = _run(composite=True, apply_finished=datetime.now(UTC))
        _out, write = await _complete(run)
        write.assert_not_awaited()


class TestAFailedResolutionAfterASuccessfulApply:
    async def test_the_run_errors_rather_than_using_the_stale_artifact(self):
        run = _run(composite=True)
        out, _ = await _complete(
            run, artifact=AsyncMock(side_effect=RuntimeError("ansible-inventory exit 1"))
        )
        assert out.status == "errored"
        assert "could not be resolved" in run.error_message
        assert "ansible-inventory exit 1" in run.error_message

    async def test_the_apply_is_still_reported_as_FINISHED(self):
        """The whole reason `configure_started_at` is stamped by hand there.

        Both "the apply failed" and "the apply succeeded and the configure
        failed" are `errored` with both apply timestamps set, so that stamp is
        the only thing telling them apart (#1988). `transition_run` sets it only
        on entering `configuring`, which this run never does -- the claim owns
        that transition -- so without the manual stamp the CLI is told the apply
        failed when it landed.
        """
        run = _run(composite=True)
        await _complete(run, artifact=AsyncMock(side_effect=RuntimeError("boom")))
        assert run.configure_started_at is not None
        assert _apply_status(run) == "finished"

    async def test_and_a_genuinely_failed_apply_still_reads_errored(self):
        """The contrast, so the stamp above cannot be made unconditional: a run
        whose APPLY failed must not report its apply as finished."""
        run = _run(composite=True)
        run.configure_started_at = None
        run.status = "errored"
        assert _apply_status(run) == "errored"


class TestWhatTheReResolutionMerges:
    async def test_the_git_half_comes_from_the_RUN_S_configuration_version(self):
        """The property the whole fix rests on, and the one easiest to lose.

        A configure following an apply must merge the platform inventory as it
        NOW stands -- the apply just wrote those rows -- with the VCS inventory
        from the configuration version THIS RUN is running against. Re-resolving
        against the branch head instead would hand the run a playbook from one
        commit and an inventory from another, which is the split #2021 exists to
        prevent, and it would do so silently.

        `resolve_for_run` already guarantees both halves: it passes
        `run.configuration_version_id` as the git source and reads the declared
        rows at call time, and it is deliberately NOT Redis-cached so a second
        call genuinely re-reads them. This pins that the hand-over goes through
        it rather than through `resolve`, which takes a branch head.
        """
        run = _run(composite=True)
        cv = uuid.uuid4()
        run.configuration_version_id = cv
        seen: dict[str, object] = {}

        async def _spy(db, r, *, limit=None):
            seen["cv"] = r.configuration_version_id
            seen["same_run"] = r is run
            return {"hosts": {}, "groups": {}, "children": {}}

        db = AsyncMock()
        db.get = AsyncMock(return_value=MagicMock())
        storage = MagicMock()
        storage.put = AsyncMock()
        with (
            patch.object(inventory_resolve, "resolve_for_run", AsyncMock(side_effect=_spy)),
            patch("terrapod.storage.get_storage", lambda: storage),
            patch.object(run_service, "transition_run", AsyncMock(side_effect=_fake_transition)),
            patch.object(run_service, "_after_apply_landed", AsyncMock()),
            patch("terrapod.redis.client.delete_job_status", new_callable=AsyncMock),
        ):
            await run_service.complete_apply(db, run)

        assert seen["cv"] == cv, "the git half was not taken from this run's configuration version"
        assert seen["same_run"] is True
        storage.put.assert_awaited_once()
