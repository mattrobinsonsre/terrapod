"""A run executes with its workspace's engine (#1523, #1536).

Found by a live run, not by a test — which is the point of recording it here.
Every unit test passed, the Job completed, and the run reported `applied`,
because a run created on a Pulumi workspace carried its own `engine="terraform"`
and so took the Terraform path: `tofu init` in a directory holding only a
`Pulumi.yaml` plans nothing and applies nothing. A green run that never invoked
Pulumi at all.

#1523 fixed it by copying the engine onto the run. #1536 removed the copy: a run
has no engine of its own, so there is nothing that can disagree with its
workspace. These pin the ways the copy could creep back.
"""

from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import terrapod

_SRC = Path(next(iter(terrapod.__path__))).resolve()


def test_no_source_reads_an_engine_off_a_run() -> None:
    """With the column gone, `run.engine` raises — but only on the path that
    runs it. Read the source so an untested path cannot hide one."""
    offenders = [
        f"{p.relative_to(_SRC)}:{n}: {line.strip()}"
        for p in sorted(_SRC.rglob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"\b(run|new_run|Run)\.engine\b", line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, "a run has no engine; use its workspace's:\n  " + "\n  ".join(offenders)


def test_the_serializer_takes_the_engine_with_no_default() -> None:
    """A default is exactly the #1523 shape: a caller that forgets gets
    `terraform`, and a Pulumi run is reported as a Terraform one."""
    from terrapod.api.routers.runs import _run_json

    param = inspect.signature(_run_json).parameters["engine"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_a_handler_without_a_workspace_reads_the_workspaces_engine() -> None:
    from terrapod.api.routers.runs import _engine_of

    ws = MagicMock()
    ws.engine = "pulumi"
    db = AsyncMock()
    db.get.return_value = ws
    run = MagicMock()
    run.workspace_id = uuid.uuid4()

    assert await _engine_of(run, db) == "pulumi"
    assert db.get.await_args.args[1] == run.workspace_id


def test_the_reconciler_joins_for_it() -> None:
    """One join per cycle rather than a lookup per in-flight run."""
    from terrapod.services import run_reconciler

    src = inspect.getsource(run_reconciler.reconcile_runs)
    assert "select(Run, Workspace.engine)" in src
    assert "strategy_for(engine)" in inspect.getsource(run_reconciler._reconcile_one)


def test_it_is_not_hardcoded_to_a_literal() -> None:
    """The other shape of the bug: an engine set, but always to Terraform."""
    from terrapod.services import run_service

    src = inspect.getsource(run_service)
    for bad in ('engine="terraform"', "engine='terraform'"):
        assert bad not in src, f"run_service hardcodes {bad}"
