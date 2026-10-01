"""Service-tier tests for module impact analysis.

Module impact analysis fires on a module's PRs/publishes against the workspaces
that CONSUME it (the `module_workspace_link`). The consuming workspace's own VCS
status is irrelevant — yet `_fetch_workspace_config` used to return None for any
non-VCS workspace, silently excluding every non-VCS consumer (CLI-driven
workspaces, and later Service Catalog instances, #535) of a VCS-linked module.
The fix reuses the workspace's latest uploaded config-version when there is no
VCS to re-fetch.
"""

import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import module_impact_service


def _non_vcs_workspace() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "catalog-instance"
    ws.vcs_connection_id = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    return ws


@pytest.mark.asyncio
async def test_fetch_config_reuses_latest_cv_for_non_vcs_workspace() -> None:
    ws = _non_vcs_workspace()
    db = AsyncMock()
    cv = MagicMock()
    cv.id = uuid.uuid4()

    with patch.object(
        module_impact_service.run_service,
        "get_latest_uploaded_cv",
        new=AsyncMock(return_value=cv),
    ) as m_latest:
        result = await module_impact_service._fetch_workspace_config(
            db, ws, MagicMock(), speculative=True
        )

    assert result == cv.id  # reused the catalog wrapper CV, not skipped
    m_latest.assert_awaited_once_with(db, ws.id)


@pytest.mark.asyncio
async def test_fetch_config_non_vcs_with_no_cv_returns_none() -> None:
    # A non-VCS workspace that has never had a CV uploaded has nothing to run.
    ws = _non_vcs_workspace()
    db = AsyncMock()

    with patch.object(
        module_impact_service.run_service,
        "get_latest_uploaded_cv",
        new=AsyncMock(return_value=None),
    ):
        result = await module_impact_service._fetch_workspace_config(db, ws, MagicMock())

    assert result is None


class TestModuleCommentUsesTheSnapshottedResult:
    """A module PR's comment must not render a plan result that has not landed.

    Two speculative runs on the same module PR, both `planned` with
    has_changes=False, rendered differently: one "No changes", the other
    "Plan finished" — which is what `_resolve_status` produces when has_changes
    is None. The enqueue happens inside the transaction that sets the status,
    so a consumer on another replica can re-read the row before the commit
    lands and see the PREVIOUS value. Which run loses the race is timing, which
    is why it looked arbitrary (#1378).

    The ordinary VCS path already snapshots the value into the payload for
    exactly this reason; this path did not.
    """

    def _payload_from_enqueue(self, run, target_status):
        """Capture what _enqueue_module_test_status puts on the queue."""
        import asyncio

        from terrapod.services import run_service

        captured = {}

        async def _fake_enqueue(name, payload, **kw):
            captured.update(payload)
            return True

        with patch("terrapod.services.scheduler.enqueue_trigger", new=_fake_enqueue):
            asyncio.run(run_service._enqueue_module_test_status(run, target_status))
        return captured

    def test_has_changes_is_snapshotted_onto_the_trigger(self):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.has_changes = False

        payload = self._payload_from_enqueue(run, "planned")

        assert "has_changes" in payload, (
            "without the snapshot the handler re-reads the row and can race the commit"
        )
        assert payload["has_changes"] is False

    def test_a_snapshotted_false_renders_no_changes_even_if_the_row_lags(self):
        """The bug, at the point where it showed: the row still says None
        (uncommitted) but the payload carries the real answer."""
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        stale_row_value = None
        snapshotted = False

        _, _, stale = _resolve_status("planned", True, stale_row_value)
        _, _, fixed = _resolve_status("planned", True, snapshotted)

        assert stale == "Plan finished"
        assert fixed == "No changes"

    def test_the_sentinel_keeps_a_genuine_none_distinguishable(self):
        """None means 'the plan did not record it' and must survive; only an
        ABSENT field falls back to the row. An older replica's trigger, raised
        mid-upgrade, carries no field at all."""
        from terrapod.services.module_impact_service import _UNSET

        assert _UNSET is not None
        assert {"has_changes": None}.get("has_changes", _UNSET) is None
        assert {}.get("has_changes", _UNSET) is _UNSET


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _member_names(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        return {m.name for m in tf.getmembers() if m.isfile()}


class TestSubmoduleScoping:
    """A submodule's PR is tested as it is published (#1583): only its own
    subdirectory, re-rooted, goes into the override tarball — not the whole
    repository. The VCS download is patched; the archive handling is real."""

    # The provider's archive wraps everything in one top-level directory.
    ARCHIVE = _tar_gz(
        {
            "org-repo-abc123/main.tf": b"# root",
            "org-repo-abc123/modules/create/main.tf": b"# create",
            "org-repo-abc123/modules/create/variables.tf": b"# vars",
            # Shares a string prefix with modules/create; must not leak in.
            "org-repo-abc123/modules/create-extra/main.tf": b"# extra",
        }
    )

    async def _run(self, subdirectory: str):
        module = MagicMock()
        module.namespace, module.name, module.provider = "default", "mg", "azurerm"
        module.subdirectory = subdirectory
        module.workspace_links = []
        pr = MagicMock()
        pr.number, pr.head_sha = 7, "abc123def456"
        # Same-repository PR: these tests are about submodule scoping, not the
        # fork trust gate. Stated explicitly because a bare MagicMock attribute
        # is truthy, which would read as "from a fork" and skip the work.
        pr.from_fork = False
        storage = MagicMock()
        storage.put = AsyncMock()
        db = AsyncMock()
        with patch.object(
            module_impact_service, "_download_archive", new=AsyncMock(return_value=self.ARCHIVE)
        ):
            await module_impact_service._create_module_test_runs(
                db, storage, module, MagicMock(provider="github"), "org", "repo", pr
            )
        return storage, db

    async def test_a_submodule_pr_archive_is_re_rooted_at_its_subdirectory(self):
        from terrapod.storage.keys import module_override_key

        storage, _ = await self._run("modules/create")
        storage.put.assert_awaited_once()
        key, archive = storage.put.await_args.args[:2]
        assert key == module_override_key("abc123def456", "default", "mg", "azurerm")
        assert _member_names(archive) == {"main.tf", "variables.tf"}

    async def test_nothing_under_the_subdirectory_means_no_override_and_no_runs(self):
        storage, db = await self._run("modules/gone")
        storage.put.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_a_root_module_takes_the_whole_repository(self):
        storage, _ = await self._run("")
        _, archive = storage.put.await_args.args[:2]
        assert _member_names(archive) == {
            "main.tf",
            "modules/create/main.tf",
            "modules/create/variables.tf",
            "modules/create-extra/main.tf",
        }


class TestForkPullRequestsOnAModuleRepository:
    """A module PR reaches further than a workspace PR (GHSA-gp5w-76rw-c452).

    It plans on every workspace that consumes the module, each with that
    workspace's own credentials. So the opt-in is read per consumer: one
    workspace's operator cannot volunteer another's secrets, and a module
    maintainer cannot volunteer any of them by merging nothing at all.
    """

    ARCHIVE = _tar_gz({"org-repo-abc123/main.tf": b"# root"})

    def _module(self, *opted_in: bool):
        module = MagicMock()
        module.namespace, module.name, module.provider = "default", "mg", "azurerm"
        module.subdirectory = ""
        links = []
        for i, allow in enumerate(opted_in):
            ws = MagicMock()
            ws.name = f"ws{i}"
            ws.allow_fork_pr_plans = allow
            links.append(MagicMock(workspace=ws))
        module.workspace_links = links
        return module

    async def _run(self, module, *, from_fork: bool):
        pr = MagicMock()
        pr.number, pr.head_sha, pr.head_ref = 7, "abc123def456", "feature"
        pr.from_fork = from_fork
        storage = MagicMock()
        storage.put = AsyncMock()
        # A plain AsyncMock makes `result.scalars()` a coroutine, so the
        # supersede sweep blows up before the gate under test is reached.
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        fetch = AsyncMock(return_value=None)  # stop each workspace after the gate
        with (
            patch.object(
                module_impact_service, "_download_archive", new=AsyncMock(return_value=self.ARCHIVE)
            ),
            patch.object(module_impact_service, "_fetch_workspace_config", new=fetch),
        ):
            await module_impact_service._create_module_test_runs(
                db, storage, module, MagicMock(provider="github"), "org", "repo", pr
            )
        planned = [call.args[1].name for call in fetch.await_args_list]
        return storage, planned

    async def test_a_fork_pr_plans_on_nothing_when_no_consumer_opted_in(self):
        storage, planned = await self._run(self._module(False, False), from_fork=True)
        assert planned == []
        # No override tarball either: storing one would be writing a fork
        # author's code into the registry's storage for nobody to use.
        storage.put.assert_not_awaited()

    async def test_a_fork_pr_plans_only_on_the_consumer_that_opted_in(self):
        storage, planned = await self._run(self._module(False, True, False), from_fork=True)
        assert planned == ["ws1"]
        storage.put.assert_awaited_once()

    async def test_a_same_repository_pr_still_plans_on_every_consumer(self):
        # The model is plan-on-PR. Narrowing it for a branch inside the
        # repository would be the regression this whole gate must not become.
        _, planned = await self._run(self._module(False, False), from_fork=False)
        assert planned == ["ws0", "ws1"]
