"""Onboarding discovery phase (#824 P2 — D2/D3).

Runs on a runner Job (with the pool's cloud workload identity) for an onboarding
session in ``querying``. Unlike plan/apply it has **no configuration version and
no state**: it writes its own minimal ``providers.tf`` for the session's
provider, then drives ``terrapod-query`` + native ``tofu`` to discover existing,
unmanaged resources and generate cleaned, import-only config, which it uploads
back to the session.

Flow (entirely read-only — no state is ever written to the workspace):

  1. providers.tf — ``required_providers`` + an empty provider block.
  2. ``tofu init`` — installs the provider (via Terrapod's mirror on the runner).
  3. per selected data-source type: ``terrapod-query query`` → collect the ids;
     ``terrapod-query import`` → candidate ``import {}`` blocks.
  4. ``tofu plan -generate-config-out`` — native config generation (may exit
     non-zero on a ConflictsWith pair; the file is still written).
  5. ``terrapod-query clean`` — deterministic, schema-driven pruning so the
     config plans import-only. This is the AI-off fallback; the optional AI pass
     layers on top for a nicer diff, never for correctness.
  6. ``tofu plan`` — verify import-only (0 add / 0 change / 0 destroy) + record.
  7. Upload the query results + cleaned config + import blocks to the session.

This module is synchronous — it is the runner Job entrypoint, not the async API,
so ``subprocess.run`` is correct here (the no-sync-in-async rule is about the
API event loop).
"""

from __future__ import annotations

import json
import re
import subprocess  # noqa: S404 — fixed argv, no shell, trusted binaries
from pathlib import Path

import structlog

from terrapod.runner.phases import uploads
from terrapod.runner.runner_config import RunnerConfig

log = structlog.get_logger("runner.discovery")

QUERY_BIN = "/usr/local/bin/terrapod-query"

# Each discovery sub-step is bounded so a wedged provider download or cloud call
# can't pin the Job past its grace budget.
_STEP_TIMEOUT = 600

# "Plan: 0 to add, 0 to change, 0 to destroy" (import lines are separate) marks a
# clean import-only result. tofu prints "N to import" alongside.
_NO_CHANGES_RE = re.compile(r"0 to add, 0 to change, 0 to destroy")


def _provider_config_hcl(provider: str) -> str:
    return (
        "terraform {\n"
        "  required_providers {\n"
        f'    {provider} = {{ source = "{provider}" }}\n'
        "  }\n"
        "}\n"
        f'provider "{provider}" {{}}\n'
    )


def _run(argv: list[str], cwd: Path, timeout: int = _STEP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_discovery(cfg: RunnerConfig, binary: str, work_dir: Path) -> int:
    """Execute D2/D3 for a discovery run. Returns a process exit code.

    The workspace is never mutated: on any failure the run errors and the
    reconciler marks the session ``errored``; on success it uploads the
    generated artifacts and the reconciler marks it ``config_ready``.
    """
    provider = cfg.onboard_provider
    types = cfg.onboard_types
    if not provider or not types:
        log.error("discovery missing provider/types", provider=provider, types=types)
        return 1

    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "providers.tf").write_text(_provider_config_hcl(provider), encoding="utf-8")

    # 2. init — installs the provider plugin (through the mirror env the caller
    # already exported). A provider that won't install is a hard failure.
    init = _run([binary, "init", "-no-color", "-input=false"], work_dir)
    if init.returncode != 0:
        log.error("discovery init failed", stderr=init.stderr[-2000:])
        return init.returncode

    # 3. Per selected type: query (D2) then import (D3).
    query_results: dict[str, object] = {}
    import_blocks: list[str] = []
    for dstype in types:
        q = _run(
            [
                QUERY_BIN,
                "query",
                "--type",
                dstype,
                "--provider-config",
                "providers.tf",
                "--tofu",
                binary,
            ],
            work_dir,
        )
        if q.returncode != 0:
            log.warning("discovery query failed", type=dstype, stderr=q.stderr[-1000:])
            continue
        try:
            query_results[dstype] = json.loads(q.stdout)
        except ValueError:
            log.warning("discovery query bad json", type=dstype)
            continue
        # import derives the managed resource type from the data-source name;
        # the query result JSON is fed on stdin.
        imp = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [QUERY_BIN, "import"],
            cwd=str(work_dir),
            input=q.stdout,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if imp.returncode == 0 and imp.stdout.strip():
            import_blocks.append(imp.stdout.strip())

    if not import_blocks:
        log.error("discovery found no importable resources", types=types)
        return 1

    (work_dir / "imports.tf").write_text("\n\n".join(import_blocks) + "\n", encoding="utf-8")

    # 4. generate-config-out — native tofu. It may exit non-zero on a
    # ConflictsWith pair; the generated file is still written, which is all we
    # need (the clean pass fixes the conflict).
    _run(
        [binary, "plan", "-generate-config-out=generated.tf", "-no-color", "-input=false"],
        work_dir,
    )
    generated = work_dir / "generated.tf"
    if not generated.exists():
        log.error("generate-config-out produced no file")
        return 1

    # 5. clean — deterministic, schema-driven pruning (the AI-off fallback).
    cleaned = work_dir / "cleaned_config.tf"
    cln = _run(
        [
            QUERY_BIN,
            "clean",
            "--config",
            "generated.tf",
            "--tofu",
            binary,
            "--dir",
            ".",
            "--out",
            "cleaned_config.tf",
        ],
        work_dir,
    )
    if cln.returncode != 0 or not cleaned.exists():
        log.error("discovery clean failed", stderr=cln.stderr[-2000:])
        return 1

    # 6. Verify import-only: swap the cleaned config in for the raw generated one
    # and plan. A correct onboarding shows 0 add / 0 change / 0 destroy; anything
    # else means a mis-derived id — still surfaced to the operator, flagged.
    generated.unlink(missing_ok=True)
    verify = _run([binary, "plan", "-no-color", "-input=false"], work_dir)
    import_only = verify.returncode == 0 and bool(_NO_CHANGES_RE.search(verify.stdout))
    log.info("discovery verify plan", import_only=import_only, rc=verify.returncode)

    # 7. Upload the artifacts back to the session (resolved server-side by the
    # discovery run id). config + imports are text; the query results are JSON.
    ok_cfg = uploads.upload_onboarding_config(cfg, cleaned)
    ok_imp = uploads.upload_onboarding_imports(cfg, work_dir / "imports.tf")
    ok_q = uploads.post_onboarding_query_results(
        cfg,
        {
            "results": query_results,
            "selected_types": types,
            "import_only": import_only,
        },
    )
    if not (ok_cfg and ok_imp and ok_q):
        log.error("discovery upload failed", config=ok_cfg, imports=ok_imp, query=ok_q)
        return 1

    log.info("discovery complete", types=len(types), import_only=import_only)
    return 0
