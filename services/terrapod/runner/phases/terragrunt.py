"""Terragrunt single-unit support for the runner (#534).

When a workspace has Terragrunt enabled, the runner invokes `terragrunt`
(wrapping the cached `tofu`/`terraform` binary) for init/plan/apply instead of
calling the binary directly. Two tiny wrapper scripts make this transparent to
the rest of the orchestrator and reconcile Terragrunt with Terrapod's
local-backend + state-via-API model:

  tg-wrapper  — used as the orchestrator's `binary`. `tg-wrapper <subcmd> …`
                execs `terragrunt <subcmd> …` with `TG_TF_PATH=<tf-wrapper>`,
                so every existing `[binary, "init"/"plan"/"apply"/"show", …]`
                call site works unchanged. (The env var is used, not the
                --tf-path flag, which Terragrunt 1.0 rejects as a global flag.)
  tf-wrapper  — what Terragrunt runs as the terraform binary (via TG_TF_PATH).
                Terragrunt invokes it from inside its working dir
                (`.terragrunt-cache/<hash>/<hash>/`). Before exec'ing the real
                tofu/terraform it drops `zzzz_terrapod_backend_override.tf`
                (`terraform { backend "local" {} }`) into that dir. tofu/tofu
                override files ALWAYS replace the backend block, so the local
                backend wins over whatever `remote_state`/`generate` Terragrunt
                produced — without editing user config. State then lands in the
                working dir, which `resolve_working_dir` discovers for capture.

Terragrunt copies every unit (with or without `terraform { source }`) into a
`.terragrunt-cache/<hash>/<hash>/` dir and runs tofu there. The orchestrator
therefore relocates the downloaded state into that dir after init
(`relocate_state`) and treats it as the working dir for plan/apply + state
capture, while the process stays chdir'd to the unit dir so terragrunt finds
`terragrunt.hcl`.

The runner image is bash-free (#167), so both wrappers are Python.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.phases.binary_verify import (
    ExecutableVerificationError,
    verify_executable,
)
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

    # Integrity gate (#607): verify the terragrunt binary against the publisher's
    # signed SHA256SUMS with our pinned key before it's run. Cache-only path, so
    # verification material comes from the Terrapod cache (same source).
    _verify_client = client or httpx.Client()
    try:
        verify_executable(
            cfg,
            "terragrunt",
            cfg.terragrunt_version or "1.0",
            dest,
            from_cache=True,
            client=_verify_client,
        )
    except ExecutableVerificationError as exc:
        raise TerragruntError(f"terragrunt verification failed: {exc}") from exc
    finally:
        if client is None:
            _verify_client.close()

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
    # tg-wrapper: terragrunt with the tf-wrapper pinned via TG_TF_PATH. The env
    # var (not the --tf-path flag) is used deliberately: Terragrunt 1.0's CLI
    # redesign rejects --tf-path as a global flag, but TG_TF_PATH is honored
    # regardless of CLI structure.
    tg_src = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"TG = {str(terragrunt_bin)!r}\n"
        f"TF_WRAPPER = {str(tf_wrapper)!r}\n"
        "os.environ['TG_TF_PATH'] = TF_WRAPPER\n"
        "os.execv(TG, [TG, *sys.argv[1:]])\n"
    )
    for path, src in ((tf_wrapper, tf_src), (tg_wrapper, tg_src)):
        path.write_text(src)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("terragrunt wrappers written", tg=str(tg_wrapper), tf=str(tf_wrapper))
    return tg_wrapper


def relocate_state(*, src: Path | str, dst: Path | str) -> bool:
    """Copy the downloaded local state from the unit dir into terragrunt's
    actual tofu working dir (the `.terragrunt-cache` subdir).

    Terrapod downloads the workspace's current `terraform.tfstate` beside the
    unit's config BEFORE init (the cache dir doesn't exist yet). Terragrunt then
    runs tofu inside the cache dir, where it reads/writes state — so without
    this the plan would see empty state and propose recreating everything, and
    apply would persist a fork. Copy (not move) so a stale leftover in the unit
    dir is harmless; overwrite any copy terragrunt itself made during its config
    download. Returns True if a state file was placed.
    """
    src_dir, dst_dir = Path(src), Path(dst)
    placed = False
    for name in ("terraform.tfstate", "terraform.tfstate.backup"):
        s = src_dir / name
        if s.exists() and s.is_file():
            shutil.copy2(s, dst_dir / name)
            placed = True
    return placed


def resolve_working_dir(unit_dir: Path | str) -> Path:
    """Resolve Terragrunt's actual tofu working dir after init.

    With `terraform { source = … }`, Terragrunt copies the module into
    `<unit>/.terragrunt-cache/<hash>/<hash>/` and runs tofu THERE, so the local
    state lands in that dir rather than the unit dir. We discover it by the
    override marker the tf-wrapper drops on every tofu invocation (unique to
    us) — robust, and version-proof now that `terragrunt-info` was removed in
    Terragrunt 1.0. In-place units (no `source`) have no cache; tofu runs in
    the unit dir, which we return as the fallback.

    Returns the directory the runner should treat as the working dir for state
    capture + the `!= local` backstop.
    """
    unit = Path(unit_dir)
    cache = unit / ".terragrunt-cache"
    if cache.is_dir():
        # The marker is dropped wherever tofu actually ran. Ignore copies that
        # happen to live under a `.terraform/` subdir; prefer the shallowest
        # real working dir.
        candidates = [
            m.parent for m in cache.rglob(_OVERRIDE_NAME) if ".terraform" not in m.parent.parts
        ]
        if candidates:
            candidates.sort(key=lambda p: len(p.parts))
            logger.info("resolved terragrunt working dir", work_dir=str(candidates[0]))
            return candidates[0]
    return unit
