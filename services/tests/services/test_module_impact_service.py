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

import httpx
import pytest

from terrapod.db.models import VCSConnection
from terrapod.services import module_impact_service


def _non_vcs_workspace() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "catalog-instance"
    ws.vcs_connection_id = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.execution_backend = "tofu"
    ws.engine_version = "1.13.2"
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
        storage.delete = AsyncMock()
        db = AsyncMock()
        no_rows = MagicMock()
        no_rows.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=no_rows)
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
        storage.delete.assert_awaited_once_with(key)

    async def test_nothing_under_the_subdirectory_means_no_override_and_no_runs(self):
        storage, db = await self._run("modules/gone")
        storage.put.assert_not_awaited()
        storage.delete.assert_not_awaited()
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
            ws.catalog_item_id = None
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
        result.scalar_one_or_none.return_value = None
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


class TestSelfContainedModulePR:
    """A module PR executes candidate root code, not a prior catalog CV."""

    async def test_first_root_cv_failure_cleans_staged_archive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            module_impact_service.catalog_service.settings.vcs, "tmpdir", str(tmp_path)
        )
        archive = _tar_gz({"main.tf": b'variable "name" {}'})

        async def chunks(key):
            yield archive

        storage = MagicMock()
        storage.get_stream = MagicMock(side_effect=chunks)
        db = MagicMock()
        with (
            patch.object(
                module_impact_service.run_service,
                "create_configuration_version",
                AsyncMock(side_effect=RuntimeError("CV unavailable")),
            ),
            pytest.raises(RuntimeError, match="CV unavailable"),
        ):
            await module_impact_service._create_root_pr_configuration(
                db,
                storage,
                _non_vcs_workspace(),
                "module_overrides/sha/default/root/aws.tar.gz",
                None,
                uuid.uuid4(),
            )
        storage.get_stream.assert_called_once()
        assert not list(tmp_path.iterdir())

    async def _run(
        self,
        tmp_path,
        monkeypatch,
        *,
        pin=None,
        from_fork=False,
        root_allowed=True,
        with_wrapper=False,
        second_root=False,
        invalid=False,
        unsafe=False,
        referenced=False,
        vcs_provider="github",
        retry_failed_status=False,
        terminal_wrapper=False,
        canceled_wrapper=False,
        retained_wrapper_without_override=False,
        broken_download=False,
        backend_constraints=None,
        candidate_input='variable "candidate_input" {}\n',
        stored_sensitive=False,
        root_options=None,
        root_variables=None,
        second_root_variables=None,
    ):
        monkeypatch.setattr(
            module_impact_service.catalog_service.settings.vcs, "tmpdir", str(tmp_path)
        )
        root = _non_vcs_workspace()
        root.catalog_item_id = uuid.uuid4()
        root.catalog_version_pin = pin
        root.catalog_update_block = {"version": "2.0.0", "reason": "inputs"}
        root.allow_fork_pr_plans = root_allowed
        root.name = "root-consumer"
        links = [MagicMock(workspace=root, workspace_id=root.id)]
        if second_root:
            another = _non_vcs_workspace()
            another.name = "second-root-consumer"
            another.catalog_item_id = uuid.uuid4()
            another.allow_fork_pr_plans = root_allowed
            links.append(MagicMock(workspace=another, workspace_id=another.id))
        if with_wrapper:
            wrapper = _non_vcs_workspace()
            wrapper.name = "wrapper-consumer"
            wrapper.catalog_item_id = None
            wrapper.allow_fork_pr_plans = True
            links.append(MagicMock(workspace=wrapper, workspace_id=wrapper.id))
        module = MagicMock(
            id=uuid.uuid4(),
            namespace="default",
            provider="aws",
            subdirectory="modules/root",
            workspace_links=links,
        )
        module.name = "root"
        module.vcs_connection_id = uuid.uuid4()
        module.vcs_repo_url = f"https://{vcs_provider}.com/owner/repo"
        module.vcs_branch = "main"
        module.vcs_last_pr_shas = {}
        item = MagicMock(self_contained=True, module_id=module.id)
        item.variable_options = root_options or []
        item.execution_backend_constraints = backend_constraints or {"tofu": ">=1.12,<2"}
        conn = MagicMock(provider=vcs_provider, status="active")
        pr = MagicMock(
            number=7, head_sha="a" * 40, head_ref="feature", title="candidate", from_fork=from_fork
        )
        candidate = candidate_input.encode()
        archive = _tar_gz(
            {
                "repo/main.tf": b"# not the submodule",
                "repo/modules/root/main.tf": b'variable "candidate_input" {'
                if invalid
                else candidate,
                "repo/modules/root/child/vars.tf": b"# child",
                "repo/modules/root-extra/main.tf": b"# other module",
                **(
                    {"repo/modules/root/terraform.tfvars": b"candidate_input = 0"} if unsafe else {}
                ),
            }
        )
        if broken_download:
            archive = b"not a gzip archive"
        blobs = {}
        stored_configs = {}
        storage = MagicMock()

        async def put(key, data, *_args):
            blobs[key] = data

        async def get_stream(key):
            yield blobs[key]

        async def put_stream(key, chunks, *, content_type):
            assert content_type == "application/gzip"
            stored_configs[key] = b"".join([chunk async for chunk in chunks])

        async def delete(key):
            blobs.pop(key, None)

        storage.put = AsyncMock(side_effect=put)
        storage.get_stream = MagicMock(side_effect=get_stream)
        storage.put_stream = AsyncMock(side_effect=put_stream)
        storage.delete = AsyncMock(side_effect=delete)
        db = MagicMock()
        rows = MagicMock()
        rows.scalars.return_value.all.return_value = []
        rows.scalar_one_or_none.return_value = None
        other_run = MagicMock()
        other_run.scalar_one_or_none.return_value = uuid.uuid4()

        async def execute(stmt):
            sql = str(stmt)
            if "module_impact_module_id" in sql and "vcs_commit_sha =" in sql and runs:
                values = stmt.compile().params.values()
                if (
                    with_wrapper
                    and wrapper.id in values
                    and any(
                        run.workspace_id == wrapper.id
                        and run.status != "canceled"
                        and run.module_impact_module_id == module.id
                        for run in runs
                    )
                ):
                    return other_run
            if (referenced or runs) and "module_overrides" in sql and "vcs_commit_sha" not in sql:
                return other_run
            return rows

        db.execute = AsyncMock(side_effect=execute)
        db.get = AsyncMock(side_effect=lambda model, _id: conn if model is VCSConnection else item)
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        cv = MagicMock(id=uuid.uuid4())
        runs = []
        saved = (
            root_variables
            if root_variables is not None
            else [
                MagicMock(
                    key="candidate_input",
                    category="terraform",
                    value="saved",
                    structured=False,
                    sensitive=stored_sensitive,
                )
            ]
        )

        async def create_run(*_args, **_kwargs):
            run = MagicMock(id=uuid.uuid4())
            run.status = "queued"
            run.workspace_id = _kwargs["workspace"].id
            run.message = _kwargs["message"]
            runs.append(run)
            return run

        with (
            patch.object(
                module_impact_service, "_download_archive", AsyncMock(return_value=archive)
            ),
            patch.object(module_impact_service, "_list_open_prs", AsyncMock(return_value=[pr])),
            patch.object(
                module_impact_service.run_service,
                "create_configuration_version",
                AsyncMock(side_effect=[cv, MagicMock(id=uuid.uuid4())]),
            ) as create_cv,
            patch.object(
                module_impact_service.run_service, "mark_configuration_uploaded", AsyncMock()
            ),
            patch.object(
                module_impact_service.run_service, "create_run", AsyncMock(side_effect=create_run)
            ) as create,
            patch.object(
                module_impact_service.run_service,
                "queue_run",
                AsyncMock(side_effect=lambda run_db, run: run),
            ),
            patch.object(
                module_impact_service,
                "_fetch_workspace_config",
                AsyncMock(return_value=uuid.uuid4()),
            ) as fetch_old,
            patch.object(
                module_impact_service.variable_service,
                "list_variables",
                AsyncMock(
                    side_effect=lambda _db, ws_id: (
                        second_root_variables
                        if second_root and ws_id == another.id and second_root_variables is not None
                        else saved
                    )
                ),
            ),
            patch.object(
                module_impact_service, "_enqueue_module_vcs_status", AsyncMock()
            ) as pending,
            patch.object(
                module_impact_service.github_service,
                "create_commit_status",
                AsyncMock(side_effect=[httpx.ConnectError("transient"), None])
                if retry_failed_status
                else AsyncMock(),
            ) as failed,
            patch.object(
                module_impact_service.gitlab_service, "create_commit_status", AsyncMock()
            ) as failed_gitlab,
        ):
            if retry_failed_status:
                await module_impact_service._poll_module_prs(db, storage, module)
                module.first_cursor = dict(module.vcs_last_pr_shas)
                if terminal_wrapper and runs:
                    runs[0].status = "applied"
                if canceled_wrapper and runs:
                    runs[0].status = "canceled"
                if retained_wrapper_without_override and runs:
                    runs[0].module_overrides = None
                await module_impact_service._poll_module_prs(db, storage, module)
            else:
                module.last_outcome = await module_impact_service._create_module_test_runs(
                    db, storage, module, conn, "owner", "repo", pr
                )
        return (
            root,
            module,
            pr,
            candidate,
            blobs,
            stored_configs,
            cv,
            runs,
            create_cv,
            create,
            fetch_old,
            pending,
            failed if vcs_provider == "github" else failed_gitlab,
            storage,
        )

    @pytest.mark.parametrize("pin", [None, "1.0.0"])
    async def test_pr_root_is_scoped_speculative_and_plan_only(self, tmp_path, monkeypatch, pin):
        (
            root,
            module,
            pr,
            candidate,
            blobs,
            stored_configs,
            cv,
            runs,
            create_cv,
            create,
            fetch_old,
            pending,
            failed,
            storage,
        ) = await self._run(tmp_path, monkeypatch, pin=pin, with_wrapper=True)
        from terrapod.storage.keys import config_version_key, module_override_key

        key = module_override_key(pr.head_sha, "default", "root", "aws")
        assert _member_names(blobs[key]) == {"main.tf", "child/vars.tf"}
        root_archive = stored_configs[config_version_key(str(root.id), str(cv.id))]
        with tarfile.open(fileobj=io.BytesIO(root_archive), mode="r:gz") as tar:
            assert tar.extractfile("main.tf").read() == candidate
        assert create_cv.await_args.kwargs == {
            "workspace_id": root.id,
            "source": "module-test",
            "auto_queue_runs": False,
            "speculative": True,
            "archive_provenance": "protected",
            "archive_module_id": module.id,
        }
        assert len(runs) == 2
        assert create.await_args_list[0].kwargs["configuration_version_id"] == cv.id
        assert all(call.kwargs["plan_only"] is True for call in create.await_args_list)
        assert all(call.kwargs["source"] == "module-test" for call in create.await_args_list)
        assert runs[0].module_overrides == {"default/root/aws": key}
        assert all(run.module_impact_module_id == module.id for run in runs)
        assert runs[0].vcs_commit_sha == pr.head_sha
        assert runs[0].vcs_pull_request_number == pr.number
        assert root.catalog_update_block == {"version": "2.0.0", "reason": "inputs"}
        assert fetch_old.await_count == 1  # Only the wrapper reuses its own CV.
        assert pending.await_count == 2
        failed.assert_not_awaited()
        storage.get_stream.assert_called_once_with(key)
        assert not list(tmp_path.iterdir())

    async def test_fork_denial_never_stages_or_plans_a_root(self, tmp_path, monkeypatch):
        (
            root,
            _,
            _,
            _,
            _,
            configs,
            _,
            runs,
            create_cv,
            create,
            fetch_old,
            pending,
            _,
            storage,
        ) = await self._run(
            tmp_path, monkeypatch, from_fork=True, root_allowed=False, with_wrapper=True
        )
        assert all(str(root.id) not in key for key in configs)
        create_cv.assert_not_awaited()
        assert len(runs) == 1
        assert create.await_args.kwargs["workspace"].name == "wrapper-consumer"
        fetch_old.assert_awaited_once()
        pending.assert_awaited_once()
        storage.get_stream.assert_not_called()

    @pytest.mark.parametrize("unsafe", [False, True])
    async def test_invalid_root_reports_failure_without_blocking_wrappers(
        self, tmp_path, monkeypatch, unsafe
    ):
        (
            root,
            _,
            pr,
            _,
            _,
            configs,
            _,
            runs,
            create_cv,
            create,
            fetch_old,
            pending,
            failed,
            storage,
        ) = await self._run(
            tmp_path, monkeypatch, invalid=not unsafe, unsafe=unsafe, with_wrapper=True
        )
        assert all(str(root.id) not in key for key in configs)
        create_cv.assert_not_awaited()
        assert len(runs) == 1
        fetch_old.assert_awaited_once()
        pending.assert_awaited_once()
        failed.assert_awaited_once()
        assert failed.await_args.args[3] == pr.head_sha
        assert failed.await_args.kwargs["context"] == f"terrapod/{root.name}"
        assert failed.await_args.kwargs["state"] == "failure"
        assert create.await_args.kwargs["workspace"].name == "wrapper-consumer"
        assert not list(tmp_path.iterdir())
        assert storage.get_stream.call_count == 1

    async def test_fork_opt_in_plans_a_pinned_root(self, tmp_path, monkeypatch):
        (
            _,
            _,
            _,
            _,
            _,
            configs,
            _,
            runs,
            create_cv,
            _,
            fetch_old,
            pending,
            failed,
            storage,
        ) = await self._run(tmp_path, monkeypatch, pin="1.0.0", from_fork=True)
        assert len(configs) == len(runs) == 1
        create_cv.assert_awaited_once()
        fetch_old.assert_not_awaited()
        pending.assert_awaited_once()
        failed.assert_not_awaited()
        assert storage.get_stream.call_count == 1

    @pytest.mark.parametrize(
        ("candidate_input", "stored_sensitive"),
        [
            ('variable "candidate_input" { sensitive = true }', False),
            ('variable "candidate_input" {}', True),
        ],
    )
    @pytest.mark.parametrize("pin", [None, "1.0.0"])
    async def test_pr_sensitive_change_blocks_root_before_queue_even_when_pinned(
        self, tmp_path, monkeypatch, candidate_input, stored_sensitive, pin
    ):
        result = await self._run(
            tmp_path,
            monkeypatch,
            candidate_input=candidate_input,
            stored_sensitive=stored_sensitive,
            pin=pin,
            with_wrapper=True,
        )
        root, _, _, _, _, configs, _, runs, create_cv, create, fetch, pending, failed, _ = result
        assert all(str(root.id) not in key for key in configs)
        assert len(runs) == 1 and runs[0].workspace_id != root.id
        create_cv.assert_not_awaited()
        assert create.await_count == fetch.await_count == pending.await_count == 1
        failed.assert_awaited_once()
        assert failed.await_args.kwargs["description"] == "Module PR candidate cannot be tested"
        assert "candidate_input" not in str(failed.await_args)

    @pytest.mark.parametrize("stored_sensitive", [False, True])
    async def test_pr_hidden_sensitive_change_is_blocked(
        self, tmp_path, monkeypatch, stored_sensitive
    ):
        result = await self._run(
            tmp_path,
            monkeypatch,
            candidate_input='variable "candidate_input" { sensitive = true }'
            if not stored_sensitive
            else 'variable "candidate_input" {}',
            stored_sensitive=stored_sensitive,
            root_options=[{"name": "candidate_input", "hidden": True, "default": "saved"}],
        )
        assert result[7] == []
        result[8].assert_not_awaited()
        result[11].assert_not_awaited()
        result[12].assert_awaited_once()
        assert result[12].await_args.kwargs["description"] == "Module PR candidate cannot be tested"
        assert "candidate_input" not in str(result[12].await_args)

    async def test_pr_valid_matching_sensitive_input_still_plans(self, tmp_path, monkeypatch):
        result = await self._run(
            tmp_path,
            monkeypatch,
            candidate_input='variable "candidate_input" { sensitive = true }',
            stored_sensitive=True,
        )
        assert len(result[7]) == 1
        result[11].assert_awaited_once()
        result[12].assert_not_awaited()

    async def test_pr_sensitive_overlay_cannot_mask_an_unmarked_root(self, tmp_path, monkeypatch):
        result = await self._run(
            tmp_path,
            monkeypatch,
            candidate_input='variable "candidate_input" {}',
            stored_sensitive=True,
            root_options=[{"name": "candidate_input", "sensitive": True}],
        )
        assert result[7] == []
        result[8].assert_not_awaited()
        result[12].assert_awaited_once()

    @pytest.mark.parametrize("problem", ["removed", "required", "invalid-choice", "hidden"])
    async def test_pr_reuses_publish_input_checks_before_creating_cv(
        self, tmp_path, monkeypatch, problem
    ):
        options = {
            "invalid-choice": [{"name": "candidate_input", "options": ["allowed"]}],
            "hidden": [{"name": "candidate_input", "hidden": True, "default": "new"}],
        }.get(problem)
        vars_for_root = {
            "removed": [MagicMock(key="old", category="native", sensitive=False)],
            "required": [],
        }.get(problem)
        result = await self._run(
            tmp_path, monkeypatch, root_options=options, root_variables=vars_for_root
        )
        assert result[7] == []
        result[8].assert_not_awaited()
        result[12].assert_awaited_once()
        assert result[12].await_args.kwargs["description"] == "Module PR candidate cannot be tested"

    async def test_pr_checks_each_instance_but_stages_archive_once(self, tmp_path, monkeypatch):
        result = await self._run(tmp_path, monkeypatch, second_root=True, second_root_variables=[])
        root = result[0]
        assert len(result[7]) == 1
        assert result[7][0].workspace_id == root.id
        assert result[9].await_args.kwargs["workspace"].id == root.id
        assert result[8].await_count == result[11].await_count == 1
        assert result[12].await_count == 1
        assert result[12].await_args.kwargs["context"] == "terrapod/second-root-consumer"
        assert result[13].get_stream.call_count == 1

    async def test_pr_input_failure_retries_status_without_duplicate_wrapper(
        self, tmp_path, monkeypatch
    ):
        result = await self._run(
            tmp_path,
            monkeypatch,
            candidate_input='variable "candidate_input" { sensitive = true }',
            with_wrapper=True,
            retry_failed_status=True,
            terminal_wrapper=True,
            retained_wrapper_without_override=True,
        )
        _, module, pr, _, _, _, _, runs, _, create, fetch, pending, failed, _ = result
        assert module.first_cursor == {}
        assert module.vcs_last_pr_shas == {"7": pr.head_sha}
        assert len(runs) == create.await_count == fetch.await_count == pending.await_count == 1
        assert failed.await_count == 2

    async def test_narrowed_catalog_runtime_does_not_plan_module_pr(self, tmp_path, monkeypatch):
        result = await self._run(tmp_path, monkeypatch, backend_constraints={"terraform": ">=1.12"})
        assert result[7] == []
        result[8].assert_not_awaited()
        result[11].assert_not_awaited()

    async def test_root_archive_is_staged_once_for_two_instances(self, tmp_path, monkeypatch):
        (
            _,
            _,
            _,
            _,
            _,
            configs,
            _,
            runs,
            create_cv,
            _,
            _,
            pending,
            failed,
            storage,
        ) = await self._run(tmp_path, monkeypatch, second_root=True)
        assert len(configs) == len(runs) == 2
        assert create_cv.await_count == pending.await_count == 2
        assert storage.get_stream.call_count == 1
        failed.assert_not_awaited()

    async def test_invalid_root_reports_gitlab_failure(self, tmp_path, monkeypatch):
        (
            _,
            _,
            _,
            _,
            blobs,
            _,
            _,
            runs,
            create_cv,
            _,
            _,
            pending,
            failed,
            storage,
        ) = await self._run(tmp_path, monkeypatch, invalid=True, vcs_provider="gitlab")
        assert runs == []
        create_cv.assert_not_awaited()
        pending.assert_not_awaited()
        failed.assert_awaited_once()
        assert failed.await_args.kwargs["state"] == "failed"
        storage.delete.assert_awaited_once()
        assert blobs == {}

    async def test_invalid_root_reports_each_permitted_instance(self, tmp_path, monkeypatch):
        (_, _, _, _, _, _, _, runs, create_cv, _, _, pending, failed, storage) = await self._run(
            tmp_path, monkeypatch, invalid=True, second_root=True
        )
        assert runs == []
        create_cv.assert_not_awaited()
        pending.assert_not_awaited()
        assert failed.await_count == 2
        assert storage.get_stream.call_count == 1

    async def test_unused_override_shared_with_another_run_is_not_deleted(
        self, tmp_path, monkeypatch
    ):
        (_, _, _, _, blobs, _, _, runs, _, _, _, _, failed, storage) = await self._run(
            tmp_path, monkeypatch, invalid=True, referenced=True
        )
        assert runs == []
        failed.assert_awaited_once()
        storage.delete.assert_not_awaited()
        assert len(blobs) == 1

    @pytest.mark.parametrize("terminal_wrapper", [False, True])
    async def test_failed_status_retries_without_requeueing_same_sha_wrapper(
        self, tmp_path, monkeypatch, terminal_wrapper
    ):
        (
            _,
            module,
            pr,
            _,
            _,
            _,
            _,
            runs,
            _,
            create,
            fetch_old,
            pending,
            failed,
            _,
        ) = await self._run(
            tmp_path,
            monkeypatch,
            invalid=True,
            with_wrapper=True,
            retry_failed_status=True,
            terminal_wrapper=terminal_wrapper,
        )
        assert module.first_cursor == {}
        assert module.vcs_last_pr_shas == {"7": pr.head_sha}
        assert len(runs) == 1
        assert create.await_count == fetch_old.await_count == pending.await_count == 1
        assert failed.await_count == 2
        assert runs[0].vcs_commit_sha == pr.head_sha

    async def test_broken_archive_repack_posts_failure_before_storing_override(
        self, tmp_path, monkeypatch
    ):
        (
            _,
            module,
            _,
            _,
            blobs,
            configs,
            _,
            runs,
            create_cv,
            _,
            _,
            pending,
            failed,
            storage,
        ) = await self._run(tmp_path, monkeypatch, broken_download=True, with_wrapper=True)
        assert module.last_outcome is True
        assert runs == [] and blobs == {} and configs == {}
        create_cv.assert_not_awaited()
        storage.put.assert_not_awaited()
        pending.assert_not_awaited()
        assert failed.await_count == 2

    async def test_canceled_sha_is_planned_again_when_pr_returns(self, tmp_path, monkeypatch):
        (
            _,
            module,
            pr,
            _,
            _,
            _,
            _,
            runs,
            _,
            create,
            fetch_old,
            pending,
            failed,
            _,
        ) = await self._run(
            tmp_path,
            monkeypatch,
            invalid=True,
            with_wrapper=True,
            retry_failed_status=True,
            canceled_wrapper=True,
        )
        assert module.vcs_last_pr_shas == {"7": pr.head_sha}
        assert len(runs) == create.await_count == fetch_old.await_count == pending.await_count == 2
        assert failed.await_count == 2

    async def test_retry_recognizes_completed_run_after_override_retention(
        self, tmp_path, monkeypatch
    ):
        (
            _,
            module,
            pr,
            _,
            _,
            _,
            _,
            runs,
            _,
            create,
            fetch_old,
            pending,
            failed,
            _,
        ) = await self._run(
            tmp_path,
            monkeypatch,
            invalid=True,
            with_wrapper=True,
            retry_failed_status=True,
            terminal_wrapper=True,
            retained_wrapper_without_override=True,
        )
        assert module.vcs_last_pr_shas == {"7": pr.head_sha}
        assert len(runs) == create.await_count == fetch_old.await_count == pending.await_count == 1
        assert failed.await_count == 2

    async def test_gitlab_normal_status_uses_the_invalid_root_check_name(self):
        from terrapod.services.vcs_provider import PullRequest

        conn = MagicMock(provider="gitlab", status="active")
        module = MagicMock(
            vcs_connection_id=uuid.uuid4(), vcs_repo_url="https://gitlab.com/owner/repo"
        )
        ws = MagicMock(id=uuid.uuid4())
        ws.name = "root-consumer"
        run = MagicMock(
            workspace_id=ws.id,
            id=uuid.uuid4(),
            vcs_commit_sha="a" * 40,
            plan_only=True,
            has_changes=False,
            vcs_pull_request_number=None,
        )
        pr = PullRequest(
            number=7, head_sha=run.vcs_commit_sha, head_ref="feature", title="test", from_fork=False
        )
        db = MagicMock()
        db.get = AsyncMock(side_effect=[conn, ws])

        with (
            patch.object(
                module_impact_service.gitlab_service, "create_commit_status", AsyncMock()
            ) as post,
            patch.object(
                module_impact_service,
                "_dispatch_parse_repo_url",
                return_value=lambda _: ("owner", "repo"),
            ),
        ):
            assert (
                await module_impact_service._post_unusable_module_pr_status(
                    conn, "owner", "repo", pr, ws, "Root archive cannot be tested"
                )
                is True
            )
            await module_impact_service._post_module_vcs_status(db, run, module, "planned")

        assert post.await_count == 2
        assert [call.kwargs["context"] for call in post.await_args_list] == [
            "terrapod/root-consumer",
            "terrapod/root-consumer",
        ]

    async def test_definitive_status_rejection_does_not_retry_on_every_poll(self):
        from terrapod.services.vcs_provider import PullRequest

        request = httpx.Request("POST", "https://example.com/status")
        response = httpx.Response(403, request=request)
        failure = httpx.HTTPStatusError("forbidden", request=request, response=response)
        conn = MagicMock(provider="github")
        ws = MagicMock()
        ws.name = "root-consumer"
        pr = PullRequest(
            number=7, head_sha="a" * 40, head_ref="branch", title="PR", from_fork=False
        )
        with patch.object(
            module_impact_service.github_service,
            "create_commit_status",
            AsyncMock(side_effect=failure),
        ):
            assert (
                await module_impact_service._post_unusable_module_pr_status(
                    conn, "owner", "repo", pr, ws, "Root archive cannot be tested"
                )
                is True
            )


class TestModuleRunAttribution:
    async def test_closed_pr_cancels_only_its_module(self):
        module = MagicMock(id=uuid.uuid4(), name="one")
        module.workspace_links = [MagicMock(workspace_id=uuid.uuid4())]
        db = MagicMock()

        async def execute(stmt):
            where = str(stmt.whereclause)
            assert "runs.module_impact_module_id" in where
            assert module.id in stmt.compile().params.values()
            return MagicMock(**{"scalars.return_value.all.return_value": []})

        db.execute = AsyncMock(side_effect=execute)
        with patch.object(module_impact_service.run_service, "cancel_run", AsyncMock()) as cancel:
            await module_impact_service._cancel_stale_module_runs(db, module, 7)
        cancel.assert_not_awaited()

    async def test_supersession_only_queries_current_module(self):
        module = MagicMock(id=uuid.uuid4(), namespace="default", provider="aws")
        module.name = "one"
        module.subdirectory = ""
        ws = _non_vcs_workspace()
        ws.catalog_item_id = None
        ws.allow_fork_pr_plans = True
        module.workspace_links = [MagicMock(workspace=ws, workspace_id=ws.id)]
        pr = MagicMock(number=7, head_sha="b" * 40, from_fork=False)
        db = MagicMock()
        statements = []

        async def execute(stmt):
            statements.append(stmt)
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            result.scalar_one_or_none.return_value = None
            return result

        db.execute = AsyncMock(side_effect=execute)
        storage = MagicMock(put=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(
                module_impact_service,
                "_download_archive",
                AsyncMock(return_value=_tar_gz({"repo/main.tf": b"# root"})),
            ),
            patch.object(
                module_impact_service, "_fetch_workspace_config", AsyncMock(return_value=None)
            ),
        ):
            await module_impact_service._create_module_test_runs(
                db, storage, module, MagicMock(), "owner", "repo", pr
            )
        supersede, dedup, _cleanup = statements
        for stmt in (supersede, dedup):
            assert "runs.module_impact_module_id" in str(stmt.whereclause)
            assert module.id in stmt.compile().params.values()
        assert "runs.vcs_commit_sha IS DISTINCT FROM" in str(supersede.whereclause)
        assert "runs.vcs_commit_sha =" in str(dedup.whereclause)

    async def test_status_after_override_retention_uses_durable_module_id(self):
        module = MagicMock(id=uuid.uuid4())
        run = MagicMock(
            source="module-test",
            module_impact_module_id=module.id,
            module_overrides=None,
            vcs_commit_sha="a" * 40,
        )
        db = MagicMock()
        db.get = AsyncMock(side_effect=[run, module])

        class Session:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_args):
                return False

        with (
            patch.object(module_impact_service, "get_db_session", return_value=Session()),
            patch.object(module_impact_service, "_post_module_vcs_status", AsyncMock()) as post,
        ):
            await module_impact_service.handle_module_test_completed(
                {"run_id": str(uuid.uuid4()), "target_status": "planned"}
            )
        post.assert_awaited_once()
        assert post.await_args.args[2] is module

    async def test_legacy_run_does_not_route_status_to_replacement_module(self):
        run = MagicMock(
            source="module-test",
            module_impact_module_id=None,
            module_overrides={"default/root/aws": "module_overrides/old.tar.gz"},
            vcs_commit_sha="a" * 40,
        )
        db = MagicMock()
        db.get = AsyncMock(return_value=run)

        class Session:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *_args):
                return False

        with (
            patch.object(module_impact_service, "get_db_session", return_value=Session()),
            patch.object(module_impact_service, "_post_module_vcs_status", AsyncMock()) as post,
        ):
            await module_impact_service.handle_module_test_completed(
                {"run_id": str(uuid.uuid4()), "target_status": "planned"}
            )
        db.get.assert_awaited_once()
        post.assert_not_awaited()


class TestSelfContainedPublish:
    """Publishing must run the new root archive, not the last catalog CV."""

    async def _trigger(
        self,
        tmp_path,
        *,
        pin=None,
        inputs=None,
        variables=None,
        archive_error=None,
        overrides=None,
        backend_constraints=None,
        previous_block_reason="inputs",
        configure_directory=None,
    ):
        ws = _non_vcs_workspace()
        ws.catalog_item_id = uuid.uuid4()
        ws.catalog_version_pin = pin
        ws.catalog_update_block = {"version": "1.9.0", "reason": previous_block_reason}
        module = MagicMock(id=uuid.uuid4(), name="root", provider="aws")
        item = MagicMock(
            self_contained=True,
            module_id=module.id,
            variable_options=overrides or [],
            execution_backend_constraints=backend_constraints or {"tofu": ">=1.12,<2"},
            configure_enabled=configure_directory is not None,
            ansible_directory=configure_directory or "",
        )
        version = MagicMock(version="2.0.0")
        result = MagicMock()
        result.scalars.return_value.all.return_value = [MagicMock(workspace=ws)]
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()
        storage = MagicMock()
        storage.put_stream = AsyncMock()
        cv = MagicMock(id=uuid.uuid4())
        run = MagicMock()
        path = tmp_path / "new.tar.gz"
        path.write_bytes(_tar_gz({"main.tf": b"# new root"}))
        staged = AsyncMock(return_value=(str(path), {"inputs": inputs or [], "outputs": []}))
        if archive_error:
            staged.side_effect = archive_error
        with (
            patch.object(module_impact_service, "get_storage", return_value=storage),
            patch.object(
                module_impact_service.catalog_service,
                "_resolve_module_version",
                new=AsyncMock(return_value=version),
            ),
            patch.object(module_impact_service.catalog_service, "_stage_root_archive", new=staged),
            patch.object(
                module_impact_service.catalog_service, "_remove_root_archive", new=AsyncMock()
            ) as cleanup,
            patch.object(
                module_impact_service.variable_service,
                "list_variables",
                new=AsyncMock(return_value=variables or []),
            ),
            patch.object(
                module_impact_service.run_service,
                "create_configuration_version",
                new=AsyncMock(return_value=cv),
            ) as create_cv,
            patch.object(
                module_impact_service.run_service, "mark_configuration_uploaded", new=AsyncMock()
            ) as mark_uploaded,
            patch.object(
                module_impact_service.run_service, "create_run", new=AsyncMock(return_value=run)
            ) as create_run,
            patch.object(
                module_impact_service.run_service, "queue_run", new=AsyncMock(return_value=run)
            ) as queue_run,
            patch.object(
                module_impact_service,
                "_fetch_workspace_config",
                new=AsyncMock(return_value=uuid.uuid4()),
            ) as fetch_old,
        ):
            runs = await module_impact_service.trigger_linked_workspace_runs(db, module, "2.0.0")
        return (
            runs,
            ws,
            cv,
            storage,
            create_cv,
            mark_uploaded,
            create_run,
            queue_run,
            fetch_old,
            cleanup,
            staged,
        )

    async def test_floating_root_publish_uploads_new_cv_without_rewriting_inputs(self, tmp_path):
        var = MagicMock(
            key="name", category="native", value="saved", structured=False, sensitive=False
        )
        (
            runs,
            ws,
            cv,
            storage,
            create_cv,
            mark,
            create_run,
            queue,
            fetch_old,
            cleanup,
            staged,
        ) = await self._trigger(
            tmp_path,
            inputs=[{"name": "name", "required": True, "default": None}],
            variables=[var],
        )
        assert len(runs) == 1
        fetch_old.assert_not_awaited()
        staged.assert_awaited_once()
        create_cv.assert_awaited_once()
        assert create_cv.await_args.kwargs["source"] == "module-publish"
        mark.assert_awaited_once()
        key, chunks = storage.put_stream.await_args.args
        from terrapod.storage.keys import config_version_key

        assert key == config_version_key(str(ws.id), str(cv.id))
        assert _member_names(b"".join([chunk async for chunk in chunks])) == {"main.tf"}
        assert create_run.await_args.kwargs["configuration_version_id"] == cv.id
        assert create_run.await_args.kwargs["source"] == "module-publish"
        assert "auto_apply" not in create_run.await_args.kwargs
        assert ws.catalog_update_block is None
        queue.assert_awaited_once()
        cleanup.assert_awaited_once()

    async def test_floating_root_publish_validates_configure_subdirectory(self, tmp_path):
        result = await self._trigger(tmp_path, configure_directory="ansible")
        staged = result[-1]
        assert staged.await_args.args[-1] == "ansible"

    @pytest.mark.parametrize(
        "problem",
        ["removed", "required", "invalid-choice", "sensitive", "sensitivity-removed", "hidden"],
    )
    async def test_changed_inputs_hold_publish_for_reconfigure(self, tmp_path, problem):
        if problem == "removed":
            inputs = []
            var = MagicMock(
                key="old", category="native", value="saved", structured=False, sensitive=False
            )
        elif problem == "required":
            inputs = [{"name": "new", "required": True, "default": None}]
            var = None
        elif problem == "invalid-choice":
            inputs = [{"name": "choice", "required": False, "default": None}]
            var = MagicMock(
                key="choice", category="native", value="invalid", structured=False, sensitive=False
            )
        elif problem == "hidden":
            inputs = [{"name": "region", "required": True, "default": None}]
            var = MagicMock(
                key="region", category="native", value="old", structured=False, sensitive=False
            )
        elif problem == "sensitivity-removed":
            inputs = [{"name": "password", "required": True, "default": None, "sensitive": False}]
            var = MagicMock(
                key="password", category="native", value="secret", structured=False, sensitive=True
            )
        else:
            inputs = [{"name": "password", "required": True, "default": None, "sensitive": True}]
            var = MagicMock(
                key="password", category="native", value="secret", structured=False, sensitive=False
            )
        if problem == "invalid-choice":
            # The same catalog overlay that drives the form is checked at publish.
            results = await self._trigger(
                tmp_path,
                inputs=inputs,
                variables=[var],
                overrides=[{"name": "choice", "options": ["valid"]}],
            )
        elif problem == "hidden":
            results = await self._trigger(
                tmp_path,
                inputs=inputs,
                variables=[var],
                overrides=[{"name": "region", "hidden": True, "default": "new"}],
            )
        else:
            results = await self._trigger(tmp_path, inputs=inputs, variables=[var] if var else [])
        runs, ws, _, storage, create_cv, _, create_run, _, fetch_old, cleanup, _ = results
        assert runs == []
        assert ws.catalog_update_block == {"version": "2.0.0", "reason": "inputs"}
        create_cv.assert_not_awaited()
        storage.put_stream.assert_not_awaited()
        create_run.assert_not_awaited()
        fetch_old.assert_not_awaited()
        cleanup.assert_awaited_once()

    async def test_narrowed_runtime_blocks_automatic_publish(self, tmp_path):
        results = await self._trigger(tmp_path, backend_constraints={"terraform": ">=1.12"})
        runs, ws, _, storage, create_cv, _, create_run, _, _, _, _ = results
        assert runs == []
        assert ws.catalog_update_block == {"version": "2.0.0", "reason": "runtime"}
        create_cv.assert_not_awaited()
        create_run.assert_not_awaited()
        storage.put_stream.assert_not_awaited()

    async def test_restored_runtime_clears_previous_block_on_pinned_publish(self, tmp_path):
        results = await self._trigger(tmp_path, pin="1.0.0", previous_block_reason="runtime")
        runs, ws, _, _, _, _, _, queue, _, _, _ = results
        assert len(runs) == 1
        assert ws.catalog_update_block is None
        queue.assert_awaited_once()

    async def test_invalid_root_archive_does_not_reuse_old_cv(self, tmp_path):
        results = await self._trigger(
            tmp_path,
            archive_error=module_impact_service.catalog_service.CatalogError("unsafe archive"),
        )
        runs, ws, _, _, create_cv, _, _, _, fetch_old, cleanup, _ = results
        assert runs == []
        assert ws.catalog_update_block == {"version": "2.0.0", "reason": "archive"}
        create_cv.assert_not_awaited()
        fetch_old.assert_not_awaited()
        cleanup.assert_not_awaited()

    async def test_pinned_root_keeps_the_existing_publish_path(self, tmp_path):
        runs, _, _, storage, create_cv, _, _, queue, fetch_old, _, staged = await self._trigger(
            tmp_path, pin="1.0.0"
        )
        assert len(runs) == 1
        fetch_old.assert_awaited_once()
        staged.assert_not_awaited()
        create_cv.assert_not_awaited()
        storage.put_stream.assert_not_awaited()
        queue.assert_awaited_once()

    @pytest.mark.parametrize("sensitive", [False, True])
    @pytest.mark.parametrize("hidden", [False, True])
    async def test_publish_sensitivity_change_blocks_even_hidden_inputs(
        self, tmp_path, sensitive, hidden
    ):
        var = MagicMock(
            key="secret_input",
            category="terraform",
            value="saved",
            structured=False,
            sensitive=sensitive,
        )
        results = await self._trigger(
            tmp_path,
            inputs=[
                {
                    "name": "secret_input",
                    "required": True,
                    "default": None,
                    "sensitive": not sensitive,
                }
            ],
            variables=[var],
            overrides=[{"name": "secret_input", "hidden": True, "default": "saved"}]
            if hidden
            else [],
        )
        assert results[0] == []
        assert results[1].catalog_update_block == {"version": "2.0.0", "reason": "inputs"}
        results[4].assert_not_awaited()
        results[6].assert_not_awaited()
