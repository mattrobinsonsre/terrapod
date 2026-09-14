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
     bits because the runner Pod runs as a non-root UID. Per-member
     utime/chmod failures on non-root are tolerated — they are cosmetic.
     An archive that cannot be READ is not tolerated: it is downloaded
     again, and if it is still unreadable the run fails naming the
     archive (#1600). Carrying on used to surface later as an unrelated-
     looking error, typically "working directory not found in config".
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
import time
import zlib
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


class ConfigurationArchiveError(RuntimeError):
    """The run's configuration archive is not a readable tar.gz, even after
    downloading it again."""


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


def _extract(tarball: Path, dest: Path) -> str | None:
    """Extract the archive; return why it is unusable, or None on success.

    `_safe_extract` lists every member before extracting any, which reads
    the whole compressed stream — so a truncated or non-gzip archive fails
    here before a single file is laid down.
    """
    if tarball.stat().st_size == 0:
        return "the download was empty"
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            _safe_extract(tar, dest)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        # tarfile.ReadError for "not a gzip file"; EOFError for a gzip
        # stream cut short; zlib.error for corrupt compressed data.
        return f"{type(exc).__name__}: {exc}"
    return None


def _describe(tarball: Path) -> str:
    """Size and leading bytes of a bad archive — enough to tell a truncated
    gzip (starts 1f 8b) from an error page or JSON stored in its place."""
    try:
        size = tarball.stat().st_size
        with tarball.open("rb") as f:
            head = f.read(16)
    except OSError as exc:
        return f"unreadable ({exc})"
    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    return f"{size} bytes, starting {head.hex(' ')} ({printable})"


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

    Raises ConfigurationArchiveError when the archive downloads but cannot
    be read, every time it is downloaded.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    # `override_dir` is where the local-backend override file goes — it
    # MUST land inside the configured working_dir so tofu/terraform
    # picks it up at init time. `strip_dir` is what we return to
    # job_entrypoint, which then passes it to
    # `working_dir.resolve_and_chdir(strip_dir, cfg.working_dir)`. The
    # descent into `working_dir` must happen exactly ONCE — here
    # we'd descend, and `resolve_and_chdir` would descend again on top,
    # producing `<work_dir>/<wd>/<wd>` (doesn't exist) and a confusing
    # "working directory '…' not found in config" error. So we keep
    # the two variables separate: `override_dir` carries the descent
    # for the override-write side-effect; `strip_dir` stays at the
    # un-descended `work_dir` and lets `resolve_and_chdir` own the
    # single canonical descent + chdir + path-traversal guard.
    override_dir = work_dir
    if cfg.working_dir:
        candidate = work_dir / cfg.working_dir
        if candidate.exists():
            override_dir = candidate

    if not cfg.has_api:
        return ConfigurationResult(downloaded=False, strip_dir=work_dir)

    # /tmp matches every other scratch artifact this orchestrator
    # writes (combined.log, plan.log, apply.log, plan.json,
    # terraform.rc, opa work dir) and matches the original bash
    # entrypoint. The previous bug was that this site computed
    # `work_dir.parent` which resolved to `/` for the default
    # WORK_DIR of `/workspace` — not writable for uid 1000.
    tarball = Path("/tmp") / "config.tar.gz"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    # download_to_file retries transient HTTP failures itself. A download
    # that succeeds but yields an unreadable archive is retried here: a
    # fetch cut short is worth another try, and if the stored archive is
    # itself damaged every attempt fails the same way and we say so.
    attempts = max(1, cfg.download_retries)
    for attempt in range(1, attempts + 1):
        logger.info("downloading configuration tarball", run_id=cfg.run_id, attempt=attempt)
        result = download_to_file(
            f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/config",
            tarball,
            headers=headers,
            api_url=cfg.api_url,
            retries=cfg.download_retries,
            retry_delay_seconds=cfg.download_retry_delay_seconds,
            client=client,
        )

        if not result.ok or not tarball.exists():
            logger.warning(
                "configuration archive download failed — see storage error above",
                status=result.status,
            )
            return ConfigurationResult(downloaded=False, strip_dir=work_dir)

        problem = _extract(tarball, work_dir)
        if problem is None:
            break
        detail = f"{problem}; {_describe(tarball)}"
        if attempt < attempts:
            logger.warning(
                "configuration archive is not a readable tar.gz — downloading it again",
                run_id=cfg.run_id,
                attempt=attempt,
                of=attempts,
                detail=detail,
            )
            tarball.unlink(missing_ok=True)
            time.sleep(cfg.download_retry_delay_seconds)
            continue
        raise ConfigurationArchiveError(
            f"the configuration archive for run {cfg.run_id} is not a readable tar.gz "
            f"after {attempts} download(s): {detail}. The archive stored for this "
            "configuration version is damaged, so retrying this run downloads it "
            "again; queue a new run so a new configuration version is built."
        )

    # Resolve override_dir again in case the configured working_dir
    # appeared during extraction (this is the common case — the tarball
    # contains the directory; the pre-extraction candidate.exists()
    # check is only useful in dev where the operator pre-populates).
    if cfg.working_dir:
        candidate = work_dir / cfg.working_dir
        if candidate.exists():
            override_dir = candidate

    _warn_on_user_override(override_dir)

    override_file = override_dir / "zzzz_terrapod_backend_override.tf"
    override_file.write_text(_LOCAL_BACKEND_OVERRIDE)
    logger.info("wrote local-backend override", path=str(override_file))

    return ConfigurationResult(
        downloaded=True,
        strip_dir=work_dir,
        override_file=override_file,
    )
