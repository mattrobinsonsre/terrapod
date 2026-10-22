"""A configure run carries the execution environment it will run in (#1972).

`Run.ee_image` existed as a column, the next-run wire sent it, the listener read
it, `build_job_spec` branched on it and the reconciler named it in its EE
timeout message -- and **nothing set it**. So every configure run reached the
runner with an empty reference, `build_job_spec` added no sidecar, no shared
`tp` volume and no `fsGroup`, and the phase died on its first write:

    OSError: [Errno 30] Read-only file system: '/tp'
    at runner/phases/configure_inventory.py:241

That is the flag-nothing-sets defect, and it was unreachable until #1974 made a
configure run creatable at all -- which is why no tier caught it and a live run
found it in one attempt.

The gate at the bottom is the one that matters for the future: `create_run` must
reach the single resolver rather than composing a reference of its own, because
`effective_ee_image` owns the empty-string fallback and the operator-label
precedence, and a second composition is a second thing to drift.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import DEFAULT_ANSIBLE_LINE
from terrapod.services import ansible_ee_service, inventory_resolve
from terrapod.services.run_service import create_run

_REPO = "ghcr.io/example/terrapod-ee"


def _ws(**kw):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "ee-ws"
    ws.auto_apply = False
    ws.engine_version = "1.11"
    ws.terragrunt_enabled = False
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.ee_resource_cpu = ""
    ws.ee_resource_memory = ""
    ws.parallelism = 10
    ws.agent_pool_links = []
    ws.plan_apply_enabled = kw.get("plan_apply_enabled", True)
    ws.configure_enabled = kw.get("configure_enabled", True)
    ws.configure_after_apply = kw.get("configure_after_apply", False)
    ws.auto_configure_enabled = False
    # The two the resolver reads. Explicit for the reason the sibling fixture
    # records: left as MagicMock attributes they are TRUTHY, so `ee_label` would
    # be a mock, the operator map would not contain it, and every run built here
    # would be refused as an unknown execution environment.
    ws.ansible_version = kw.get("ansible_version", "")
    ws.ansible_execution_environment = kw.get("ansible_execution_environment", "")
    return ws


async def _make(ws, **kw) -> dict:
    """Create a run and return the kwargs `Run(...)` was built with."""
    db = AsyncMock(spec=AsyncSession)
    with (
        patch("terrapod.services.run_service.Run") as MockRun,
        patch.object(inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))),
    ):
        inst = MockRun.return_value
        inst.id = uuid.uuid4()
        inst.status = "pending"
        await create_run(db, ws, message="m", **kw)
        MockRun.assert_called_once()
        return MockRun.call_args[1]


@pytest.mark.asyncio
class TestAConfigureCarriesItsEnvironment:
    async def test_a_standalone_configure_gets_an_ee_image(self):
        with patch.object(ansible_ee_service.settings.ansible, "ee_repository", _REPO):
            kwargs = await _make(_ws(plan_apply_enabled=False), is_configure=True)
        assert kwargs["is_configure"] is True
        assert kwargs["configure_after_apply"] is False
        assert kwargs["ee_image"] == f"{_REPO}:{DEFAULT_ANSIBLE_LINE}"

    async def test_a_composite_configure_gets_one_too(self):
        # Resolved from the workspace rather than requested, and it needs the EE
        # just as much -- the configure half runs in the same sidecar.
        with patch.object(ansible_ee_service.settings.ansible, "ee_repository", _REPO):
            kwargs = await _make(_ws(configure_after_apply=True))
        assert kwargs["configure_after_apply"] is True
        assert kwargs["ee_image"] == f"{_REPO}:{DEFAULT_ANSIBLE_LINE}"

    async def test_the_workspaces_own_line_is_used(self):
        with patch.object(ansible_ee_service.settings.ansible, "ee_repository", _REPO):
            kwargs = await _make(
                _ws(plan_apply_enabled=False, ansible_version="2.16"), is_configure=True
            )
        assert kwargs["ee_image"] == f"{_REPO}:2.16"

    async def test_an_operator_label_wins_outright(self):
        with (
            patch.object(ansible_ee_service.settings.ansible, "ee_repository", _REPO),
            patch.object(
                ansible_ee_service.settings.ansible,
                "execution_environments",
                {"hardened": "registry.example.com/mine:1"},
            ),
        ):
            kwargs = await _make(
                _ws(
                    plan_apply_enabled=False,
                    ansible_version="2.16",
                    ansible_execution_environment="hardened",
                ),
                is_configure=True,
            )
        assert kwargs["ee_image"] == "registry.example.com/mine:1"

    async def test_an_unknown_label_is_refused_rather_than_silently_defaulted(self):
        # Falling back to the built-in line here would run the playbook in an
        # environment the operator did not choose.
        with patch.object(ansible_ee_service.settings.ansible, "execution_environments", {}):
            with pytest.raises(ansible_ee_service.UnknownExecutionEnvironment):
                await _make(
                    _ws(plan_apply_enabled=False, ansible_execution_environment="gone"),
                    is_configure=True,
                )


@pytest.mark.asyncio
class TestAPlanOrApplyCarriesNone:
    async def test_a_plan_apply_run_gets_no_ee_image(self):
        """The negative half, and it is not cosmetic.

        `build_job_spec` adds the sidecar, the shared volume and an `fsGroup`
        whenever this is non-empty, so a value here would put an ansible
        container beside every single terraform plan -- which is exactly the
        "terraform users pay nothing" property #1972 is required to keep.
        """
        with patch.object(ansible_ee_service.settings.ansible, "ee_repository", _REPO):
            kwargs = await _make(_ws(configure_enabled=False))
        assert kwargs["is_configure"] is False
        assert kwargs["ee_image"] == ""


class TestTheResolverIsTheOnlyProducer:
    def test_create_run_reaches_the_shared_resolver(self):
        src = pathlib.Path(create_run.__code__.co_filename).read_text()
        fn = next(
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "create_run"
        )
        called = {
            getattr(c.func, "attr", None) or getattr(c.func, "id", None)
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
        }
        assert "effective_ee_image" in called, (
            "`create_run` no longer calls `effective_ee_image`. That call is the ONLY "
            "thing that populates `Run.ee_image`, and with it empty every configure "
            "Job is built without its execution-environment sidecar and dies on "
            "`/tp` being read-only (#1972)."
        )

    def test_it_does_not_compose_a_reference_of_its_own(self):
        """A second composition would drift from the resolver's own rules.

        The resolver owns two of them: `""` means the deployment default rather
        than an absence (#2010), and an operator label beats the line outright.
        """
        src = pathlib.Path(create_run.__code__.co_filename).read_text()
        assert "ee_repository" not in src, (
            "`run_service` reads `ee_repository` directly. The reference is "
            "`effective_ee_image`'s to compose -- see its docstring and "
            "`test_ee_image_resolution.py`."
        )
