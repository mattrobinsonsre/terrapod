"""Terragrunt single-unit support for the runner (#534).

When a workspace has Terragrunt enabled, the runner invokes `terragrunt`
(wrapping the cached `tofu`/`terraform` binary) for init/plan/apply instead of
calling the binary directly. Two tiny wrapper scripts make this transparent to
the rest of the orchestrator and reconcile Terragrunt with Terrapod's
local-backend + state-via-API model:

  tg-wrapper  — used as the orchestrator's `binary`. `tg-wrapper <subcmd> …`
                execs `terragrunt --tf-path=<tf-wrapper> <subcmd> …`, so every
                existing `[binary, "init"/"plan"/"apply"/"show", …]` call site
                works unchanged.
  tf-wrapper  — passed to terragrunt via `--tf-path`. Terragrunt invokes it as
                the terraform binary from inside its working dir
                (`.terragrunt-cache/<hash>/<module>/`). Before exec'ing the real
                tofu/terraform it drops `zzzz_terrapod_backend_override.tf`
                (`terraform { backend "local" {} }`) into that dir. tofu/tofu
                override files ALWAYS replace the backend block, so the local
                backend wins over whatever `remote_state`/`generate` Terragrunt
                produced — without editing user config. State then lands in the
                working dir, which `resolve_working_dir` discovers for capture.

The runner image is bash-free (#167), so both wrappers are Python.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.terragrunt")

# Name of the override file the tf-wrapper drops; the zzzz prefix sorts last so
# it wins tofu's override-file merge (same convention as the non-terragrunt
# path's backend neutralisation).
_OVERRIDE_NAME = "zzzz_terrapod_backend_override.tf"
_LOCAL_BACKEND = 'terraform {\n  backend "local" {}\n}\n'


class TerragruntError(RuntimeError):
    """Fatal terragrunt setup failure. Orchestrator propagates."""


def _terragrunt_cache_url(cfg: RunnerConfig) -> str:
    # Partial versions (e.g. "1.0") are resolved by the binary-cache router.
    version = cfg.terragrunt_version or "1.0"
    return f"{cfg.api_url}/api/terrapod/v1/binary-cache/terragrunt/{version}/{cfg.os}/{cfg.arch}"


def download_terragrunt(
    cfg: RunnerConfig,
    *,
    bin_dir: Path = Path("/tmp/bin"),
    client: httpx.Client | None = None,
) -> Path:
    """Fetch the terragrunt binary from Terrapod's binary cache.

    Terragrunt ships a BARE per-platform binary (not a zip), so there is no
    extraction step — the downloaded file IS the executable. Returns its path.
    Falls back to a bare `terragrunt` on PATH for degenerate dev invocations
    with no API URL (mirrors `binary.download_binary`).
    """
    if not cfg.api_url:
        logger.info("no API URL — expecting terragrunt on PATH")
        return Path("terragrunt")

    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "terragrunt"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
    url = _terragrunt_cache_url(cfg)
    logger.info(
        "downloading terragrunt from cache",
        version=cfg.terragrunt_version,
        os=cfg.os,
        arch=cfg.arch,
    )
    result = download_to_file(
        url,
        dest,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )
    if not result.ok:
        raise TerragruntError(
            f"terragrunt binary cache fetch failed (HTTP {result.status}) for "
            f"{cfg.terragrunt_version or '1.0'} {cfg.os}/{cfg.arch}."
        )
    dest.chmod(0o755)
    logger.info("terragrunt ready", path=str(dest))
    return dest


def write_wrappers(
    *,
    terragrunt_bin: Path | str,
    real_tf_bin: Path | str,
    dest_dir: Path = Path("/tmp/bin"),
) -> Path:
    """Write the tf-wrapper + tg-wrapper scripts. Returns the tg-wrapper path
    (use it as the orchestrator's `binary`)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tf_wrapper = dest_dir / "tp-tf-wrapper"
    tg_wrapper = dest_dir / "tp-tg-wrapper"

    # tf-wrapper: drop the local-backend override into the tofu working dir,
    # then exec the real binary. Handles both CWD-based and `-chdir=`-based
    # invocations so it works regardless of how Terragrunt launches tofu.
    tf_src = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"REAL = {str(real_tf_bin)!r}\n"
        f"OVERRIDE = {_OVERRIDE_NAME!r}\n"
        f"CONTENTS = {_LOCAL_BACKEND!r}\n"
        "target = '.'\n"
        "for a in sys.argv[1:]:\n"
        "    if a.startswith('-chdir='):\n"
        "        target = a[len('-chdir='):]\n"
        "try:\n"
        "    with open(os.path.join(target, OVERRIDE), 'w') as f:\n"
        "        f.write(CONTENTS)\n"
        "except OSError:\n"
        "    pass\n"
        "os.execv(REAL, [REAL, *sys.argv[1:]])\n"
    )
    # tg-wrapper: terragrunt with --tf-path pinned to the tf-wrapper.
    tg_src = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"TG = {str(terragrunt_bin)!r}\n"
        f"TF_WRAPPER = {str(tf_wrapper)!r}\n"
        "os.execv(TG, [TG, '--tf-path', TF_WRAPPER, *sys.argv[1:]])\n"
    )
    for path, src in ((tf_wrapper, tf_src), (tg_wrapper, tg_src)):
        path.write_text(src)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("terragrunt wrappers written", tg=str(tg_wrapper), tf=str(tf_wrapper))
    return tg_wrapper


def resolve_working_dir(tg_wrapper: Path | str, *, cwd: str | None = None) -> Path | None:
    """Resolve Terragrunt's actual tofu working dir via `terragrunt-info`.

    With `terraform { source = … }`, Terragrunt runs tofu inside
    `.terragrunt-cache/<hash>/<module>/` rather than the unit dir, so the
    local state file lands there. `terragrunt terragrunt-info` reports the
    `WorkingDir` as JSON. Returns the resolved path, or None if it can't be
    determined (caller falls back to the unit dir).
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is operator-controlled wrapper
            [str(tg_wrapper), "terragrunt-info"],
            check=False,
            capture_output=True,
            timeout=60,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("terragrunt-info failed", err=str(exc))
        return None
    if proc.returncode != 0:
        logger.warning(
            "terragrunt-info returned non-zero",
            rc=proc.returncode,
            stderr=proc.stderr[:300].decode("utf-8", errors="replace"),
        )
        return None
    try:
        info = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("terragrunt-info JSON parse failed", err=str(exc))
        return None
    work = info.get("WorkingDir")
    if not work:
        return None
    p = Path(work)
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    return p if p.is_dir() else None
