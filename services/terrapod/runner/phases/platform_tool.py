"""Phase: fetch a platform tool (opa / trivy / checkov) from the binary cache (#1208).

These three used to be baked into the runner image. They are now pulled through
the same cache that serves terraform/tofu, so the version is an operator-set Helm
value and an upstream fix reaches a deployment with a `helm upgrade` instead of
waiting for a Terrapod release.

**Fetch lazily, and only what the run needs.** `opa` is fetched only once the
policy bundle turns out to contain applicable sets; `trivy` only when it is the
selected scan engine. Most runs fetch neither, and none of them pays for a tool
it will not execute.

**The policy path fails closed.** `ensure_tool` raises rather than returning a
bare name to fall back on. A policy that cannot be evaluated because its binary
did not arrive must block the run — quietly passing a gate would be a worse
defect than the CVE exposure this change removes. The caller decides severity:
`opa.py` treats the raise as fatal, the scan phase records an errored result and
lets the server's fail-closed gate handle it.

The archive shapes are declared here rather than imported from
`services/platform_tools.py`, because the server module imports `terrapod.config`
which the runner image deliberately does not carry. The two are kept honest by
`tests/runner/test_platform_tool.py`, which imports both and asserts they agree.
"""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.platform_tool")


class PlatformToolsUnsupported(Exception):
    """The API has no platform-tool cache endpoint (it predates the feature).

    Distinct from PlatformToolError, which means the fetch was possible and
    went wrong. This one means "do not ask" — the deployment is an older
    supported release whose runner image still carried the binaries.
    """


class PlatformToolError(RuntimeError):
    """The tool could not be fetched or unpacked.

    Never swallowed into a bare-name fallback: there is no binary on PATH to
    fall back to any more, so pretending otherwise would surface as a confusing
    "not found" at exec time instead of the real cause.
    """


@dataclass(frozen=True)
class _Unpack:
    """What the cached object holds and where the executable is inside it."""

    kind: str  # "raw" | "targz" | "zip"
    member: str  # path within the archive ("" for raw)


UNPACK: dict[str, _Unpack] = {
    "opa": _Unpack(kind="raw", member=""),
    "trivy": _Unpack(kind="targz", member="trivy"),
    "checkov": _Unpack(kind="zip", member="dist/checkov"),
}


def _versions_url(cfg: RunnerConfig) -> str:
    return f"{cfg.api_url}/api/terrapod/v1/platform-tools"


def _cache_url(cfg: RunnerConfig, tool: str, version: str) -> str:
    return f"{cfg.api_url}/api/terrapod/v1/binary-cache/{tool}/{version}/{cfg.os}/{cfg.arch}"


def fetch_versions(cfg: RunnerConfig, *, client: httpx.Client | None = None) -> dict[str, str]:
    """The versions this deployment pins, keyed by tool.

    One small read per Job — the caller memoises. Raises PlatformToolError on
    anything unusable: without a version there is nothing to ask the cache for,
    and guessing one would fetch the wrong binary rather than fail.
    """
    own = client is None
    c = client or httpx.Client(timeout=30)
    try:
        headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
        resp = c.get(_versions_url(cfg), headers=headers)
        if resp.status_code == 404:
            # The endpoint is new in the release that stopped baking these
            # binaries in. A 404 means the API predates it — a supported skew
            # (docs/versioning-and-support.md promises a newer runner works
            # against an older API within the window), so this is "the API has
            # nothing to tell me", not a failure. The caller falls back to
            # whatever is on PATH, which on such a deployment is the baked-in
            # binary that older API expects to be there.
            #
            # ONLY 404. A 5xx, a timeout or an auth error means the endpoint
            # exists and something is wrong, and those must stay fatal — a
            # policy gate that cannot be evaluated must never be skipped.
            raise PlatformToolsUnsupported(
                "this Terrapod API predates the platform-tool cache endpoint"
            )
        if resp.status_code != 200:
            raise PlatformToolError(
                f"could not read the pinned platform-tool versions from the API "
                f"(HTTP {resp.status_code})"
            )
        attrs = (resp.json().get("data") or {}).get("attributes") or {}
    except (PlatformToolError, PlatformToolsUnsupported):
        raise
    except Exception as exc:  # noqa: BLE001 — network/JSON, all equally fatal here
        raise PlatformToolError(f"could not read the pinned platform-tool versions: {exc}") from exc
    finally:
        if own:
            c.close()

    versions = {tool: attrs.get(f"{tool}-version", "") for tool in ("opa", "trivy", "checkov")}
    if not any(versions.values()):
        raise PlatformToolError(
            "the API reported no platform-tool versions — the deployment's "
            "registry.platform_tools config is missing or empty"
        )
    return versions


def _extract(archive: Path, spec: _Unpack, dest: Path) -> None:
    """Pull the executable out of whatever upstream shipped."""
    if spec.kind == "raw":
        archive.replace(dest)
        return

    if spec.kind == "targz":
        try:
            with tarfile.open(archive, "r:gz") as tf:
                member = tf.extractfile(spec.member)
                if member is None:
                    raise PlatformToolError(f"{spec.member} not found in {archive.name}")
                dest.write_bytes(member.read())
        except (tarfile.TarError, OSError) as exc:
            raise PlatformToolError(f"could not extract {archive.name}: {exc}") from exc
        return

    try:
        with zipfile.ZipFile(archive) as zf:
            dest.write_bytes(zf.read(spec.member))
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise PlatformToolError(f"could not extract {archive.name}: {exc}") from exc


def ensure_tool(
    cfg: RunnerConfig,
    tool: str,
    *,
    versions: dict[str, str] | None = None,
    tmp_dir: Path = Path("/tmp"),
    bin_dir: Path = Path("/tmp/bin"),
    client: httpx.Client | None = None,
) -> Path:
    """Fetch `tool` if it isn't already on disk, and return its path.

    Idempotent within a Job: the plan and apply phases of the same run reuse the
    first fetch. Raises PlatformToolError on any failure — see the module
    docstring for why there is no fallback.
    """
    if tool not in UNPACK:
        raise PlatformToolError(f"not a platform tool: {tool!r}")

    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / tool
    if dest.exists():
        return dest

    if not cfg.api_url:
        raise PlatformToolError(
            f"cannot fetch {tool}: no Terrapod API URL is configured for this runner"
        )

    try:
        resolved = (versions or fetch_versions(cfg, client=client)).get(tool, "")
    except PlatformToolsUnsupported:
        # A runner newer than its API — supported skew, and the API is from a
        # release whose runner image still shipped these binaries. An on-PATH
        # copy is the correct answer here, not a silent downgrade: there is
        # nothing to fetch because that deployment never expected a fetch.
        on_path = shutil.which(tool)
        if on_path:
            logger.info("using on-PATH platform tool (API predates the cache)", tool=tool)
            return Path(on_path)
        raise PlatformToolError(
            f"{tool} is not available: this Terrapod API predates the platform-tool "
            f"cache endpoint and no {tool} binary is on PATH"
        ) from None
    if not resolved:
        raise PlatformToolError(f"the deployment pins no version for {tool}")

    spec = UNPACK[tool]
    archive = tmp_dir / f"{tool}.download"
    logger.info("fetching platform tool", tool=tool, version=resolved, arch=cfg.arch)
    result = download_to_file(
        _cache_url(cfg, tool, resolved),
        archive,
        headers={"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {},
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )
    if not result.ok:
        raise PlatformToolError(
            f"could not download {tool} {resolved} for {cfg.os}/{cfg.arch} from the "
            f"binary cache (HTTP {result.status}). The API verifies the publisher "
            f"checksum before caching, so a failure here is a fetch problem, not a "
            f"trust one — check the API's upstream reach, or pre-warm the cache if "
            f"the deployment is sealed."
        )

    _extract(archive, spec, dest)
    dest.chmod(0o755)
    try:
        archive.unlink()
    except OSError:
        pass
    logger.info("platform tool ready", tool=tool, version=resolved, path=str(dest))
    return dest


def tool_path_or_name(
    cfg: RunnerConfig,
    tool: str,
    *,
    versions: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> tuple[str, str | None]:
    """`ensure_tool` for callers that must not raise.

    Returns `(path, None)` on success or `("", reason)` on failure, so a
    best-effort caller (the scan phase) can record why it produced nothing
    instead of crashing the run. The reason is operator-facing text.
    """
    try:
        return str(ensure_tool(cfg, tool, versions=versions, client=client)), None
    except PlatformToolError as exc:
        return "", str(exc)
