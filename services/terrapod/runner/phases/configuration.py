"""Phase: download + extract the run's configuration tarball, then
write the local-backend override file.

Port of the `# --- Download configuration archive ---` block of
docker/runner-entrypoint.sh (lines ~523–604 in the v0.31.x tree).

Three sub-steps, kept together because they share state and only the
combination is meaningful:

  1. Download `/runs/{run_id}/artifacts/config` from the API.
  2. Extract under work_dir, preserving the user's directory layout.
     `--no-same-owner` equivalent: tarfile defaults to current uid,
     `--no-same-permissions` equivalent: we strip the setuid/setgid
     bits because the runner Pod runs as a non-root UID. Tar errors
     for utime/chmod warnings on the "." entry are tolerated — those
     are cosmetic on non-root and tofu will fail later if extraction
     actually didn't lay files down.
  3. Write `zzzz_terrapod_backend_override.tf` into the STRIP_DIR (the
     working-directory subpath inside the extracted tree, or the root
     if no working-directory is set). The zzzz prefix sorts last in
     the override-file merge order so our `terraform { backend
     "local" {} }` wins against any user override file. See #346 for
     the backstop logic.
"""

from __future__ import annotations

import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.configuration")


_LOCAL_BACKEND_OVERRIDE = """\
# Terrapod runner: force local backend for in-runner execution.
# Override files (*_override.tf) are merged by terraform/tofu with
# replacement semantics over the main config — this displaces any
# `cloud {}` or `backend "x" {}` declared in the main config. The
# `zzzz` prefix makes this file sort last so it wins the override merge.
terraform {
  backend "local" {}
}
"""


@dataclass
class ConfigurationResult:
    """What the orchestrator needs to know about the extracted config.

    `strip_dir` is the directory that actually contains the .tf files
    — equals `work_dir` for root workspaces, or
    `work_dir / working_directory` for monorepo subpath workspaces.
    Phase 2+ (init / plan / apply) chdir here.
    """

    downloaded: bool
    strip_dir: Path
    override_file: Path | None = None


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Strip setuid/setgid + restrict members to within `dest`."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        # tarfile.SafetyError covers absolute paths and `..` traversal
        # but we belt-and-brace explicitly: refuse anything that would
        # resolve outside of `dest_resolved`.
        target = (dest / member.name).resolve()
        if not target.is_relative_to(dest_resolved):
            logger.warning("refusing to extract path traversal", member=member.name)
            continue
        # Strip setuid/setgid/sticky. Keep RWX bits as-is.
        if member.mode is not None:
            member.mode &= ~(stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        try:
            tar.extract(member, dest, set_attrs=True)
        except (PermissionError, OSError) as exc:
            # BusyBox tar tolerated utime/chmod failures on non-root;
            # so do we. tofu will fail later if files are missing.
            logger.debug("tar member extract warning", member=member.name, err=str(exc))


def _warn_on_user_override(strip_dir: Path) -> None:
    """If the user committed their own *_override.tf declaring a
    backend/cloud block, log it so operators see why their override is
    being shadowed by Terrapod's. Cosmetic — extraction does not stop."""
    for override in sorted(strip_dir.glob("*_override.tf")):
        if override.name == "zzzz_terrapod_backend_override.tf":
            continue
        try:
            text = override.read_text()
        except OSError:
            continue
        # Quick textual sniff — same regex as the bash version.
        if "terraform" in text and ("backend " in text or "cloud " in text or "cloud{" in text):
            logger.info(
                "user override declares backend/cloud block — Terrapod's "
                "local-backend override takes precedence",
                user_override=str(override),
            )
    bare = strip_dir / "override.tf"
    if bare.exists():
        try:
            text = bare.read_text()
        except OSError:
            return
        if "terraform" in text and ("backend " in text or "cloud " in text or "cloud{" in text):
            logger.info(
                "user override declares backend/cloud block — Terrapod's "
                "local-backend override takes precedence",
                user_override=str(bare),
            )


def download_configuration(
    cfg: RunnerConfig,
    *,
    work_dir: Path,
    client: httpx.Client | None = None,
) -> ConfigurationResult:
    """Acquire and unpack the run's configuration tarball.

    Without API context (degenerate dev invocations) returns
    `downloaded=False` and trusts the operator pre-populated the
    workspace. Matches the bash behaviour at line 524.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    strip_dir = work_dir
    if cfg.working_dir:
        candidate = work_dir / cfg.working_dir
        if candidate.exists():
            strip_dir = candidate

    if not cfg.has_api:
        return ConfigurationResult(downloaded=False, strip_dir=strip_dir)

    tarball = work_dir.parent / "config.tar.gz"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    logger.info("downloading configuration tarball", run_id=cfg.run_id)
    result = download_to_file(
        f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/config",
        tarball,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )

    if not result.ok or not tarball.exists() or tarball.stat().st_size == 0:
        logger.warning(
            "configuration archive download failed — see storage error above",
            status=result.status,
        )
        return ConfigurationResult(downloaded=False, strip_dir=strip_dir)

    try:
        with tarfile.open(tarball, "r:gz") as tar:
            _safe_extract(tar, work_dir)
    except tarfile.TarError as exc:
        # Match bash: tolerate but log. tofu will fail later if needed.
        logger.warning("tar extract reported a problem", err=str(exc))

    # Resolve strip_dir again in case it appeared during extraction.
    if cfg.working_dir:
        candidate = work_dir / cfg.working_dir
        if candidate.exists():
            strip_dir = candidate

    _warn_on_user_override(strip_dir)

    override_file = strip_dir / "zzzz_terrapod_backend_override.tf"
    override_file.write_text(_LOCAL_BACKEND_OVERRIDE)
    logger.info("wrote local-backend override", path=str(override_file))

    return ConfigurationResult(
        downloaded=True,
        strip_dir=strip_dir,
        override_file=override_file,
    )
