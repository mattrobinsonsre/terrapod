"""Phase: download the terraform/tofu binary from Terrapod's binary cache.

Port of the `# --- Download binary from cache ---` block of
docker/runner-entrypoint.sh (lines ~464–521 in the v0.31.x tree).

Flow:
  1. Construct the binary-cache URL from (api_url, backend, version, os, arch).
  2. Download the zip via the redirect-aware downloader. If the cache
     returns a non-2xx, fall back to the upstream release URL
     (releases.hashicorp.com / GitHub releases). The upstream fallback
     requires a fully-qualified x.y.z version — if `version` is partial
     (e.g. "1.11") raise BinaryDownloadError because the cache normally
     resolves partial versions, so getting here with a partial version
     means the cache is broken AND we'd 404 upstream anyway.
  3. Validate zip magic bytes before unpacking — a presigned-URL storage
     error often returns an HTML/XML error body that would otherwise be
     unzipped as garbage and confuse the next phase (#338).
  4. Extract to /tmp/bin/.

Returns the absolute path to the extracted binary. Callers set TP_BIN
to this path so the bash sub-phase finds it on PATH equivalent.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.binary")


_VERSION_FULL_RE = re.compile(r"^\d+\.\d+\.\d+([.\-+][\w.\-+]+)?$")


class BinaryDownloadError(RuntimeError):
    """Hard failure assembling the runner binary. Phase orchestrator
    converts to a process exit so the listener can mark the run errored."""


def _binary_cache_url(cfg: RunnerConfig) -> str:
    return (
        f"{cfg.api_url}/api/terrapod/v1/binary-cache/"
        f"{cfg.backend}/{cfg.version}/{cfg.os}/{cfg.arch}"
    )


def _upstream_url(cfg: RunnerConfig) -> str:
    if cfg.backend == "terraform":
        return (
            f"https://releases.hashicorp.com/terraform/{cfg.version}/"
            f"terraform_{cfg.version}_{cfg.os}_{cfg.arch}.zip"
        )
    return (
        f"https://github.com/opentofu/opentofu/releases/download/v{cfg.version}/"
        f"tofu_{cfg.version}_{cfg.os}_{cfg.arch}.zip"
    )


def _zip_valid(path: Path) -> bool:
    """PK\x03\x04 magic check. A storage 4xx body looks like XML — we
    catch that here before unzip would garble it."""
    try:
        with path.open("rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def download_binary(
    cfg: RunnerConfig,
    *,
    tmp_dir: Path = Path("/tmp"),
    bin_dir: Path = Path("/tmp/bin"),
    client: httpx.Client | None = None,
) -> Path:
    """Acquire and extract the runner binary. Returns its on-disk path.

    Without a Terrapod API URL or version (degenerate dev invocations)
    we trust the binary is on PATH and return that bare name — matches
    bash behaviour at line 503–506.
    """
    if not cfg.api_url or not cfg.version:
        logger.info("no API URL or version — expecting binary on PATH", backend=cfg.backend)
        return Path(cfg.backend)

    zip_path = tmp_dir / f"{cfg.backend}.zip"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    cache_url = _binary_cache_url(cfg)
    logger.info(
        "downloading binary from cache",
        backend=cfg.backend,
        version=cfg.version,
        os=cfg.os,
        arch=cfg.arch,
    )
    result = download_to_file(
        cache_url,
        zip_path,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )

    if not result.ok:
        logger.warning(
            "binary cache unavailable — trying upstream",
            status=result.status,
            backend=cfg.backend,
            version=cfg.version,
        )
        if not _VERSION_FULL_RE.match(cfg.version):
            raise BinaryDownloadError(
                "Upstream fallback needs a fully-qualified version but got "
                f"{cfg.version!r}. The binary cache normally resolves partial "
                "versions; getting here with a non-exact version means the "
                "cache request failed AND the version was never pinned. Set "
                "the workspace version to an exact x.y.z, or fix the "
                "runner->API binary-cache path. See terrapod issue #338."
            )
        upstream_result = download_to_file(
            _upstream_url(cfg),
            zip_path,
            headers={},
            api_url="",
            retries=cfg.download_retries,
            retry_delay_seconds=cfg.download_retry_delay_seconds,
            client=client,
        )
        if not upstream_result.ok:
            raise BinaryDownloadError(
                "Upstream fallback also failed "
                f"(HTTP {upstream_result.status}). Cache + upstream both "
                "unreachable for "
                f"{cfg.backend} {cfg.version} {cfg.os}/{cfg.arch}."
            )

    if not _zip_valid(zip_path):
        raise BinaryDownloadError(
            f"Downloaded file at {zip_path} is not a valid zip archive. "
            "This usually means the presigned storage URL returned an error. "
            "Check that the API storage backend region/endpoint is correct."
        )

    bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(bin_dir)
    except zipfile.BadZipFile as exc:
        raise BinaryDownloadError(f"Failed to extract {zip_path}: {exc}") from exc

    binary_path = bin_dir / cfg.backend
    binary_path.chmod(0o755)
    logger.info("binary ready", path=str(binary_path))
    return binary_path
