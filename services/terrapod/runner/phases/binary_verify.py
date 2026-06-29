"""Runner-side executable verification (#607).

Before the runner *executes* a terraform/tofu/terragrunt binary, it re-verifies
that binary against the publisher's signed SHA256SUMS using a **pinned** public
key baked into the runner image. This is the only publisher-authenticity check
on the executed CLI binary (nothing downstream verifies it), and it closes the
cache→runner link the API-side check (#607) cannot cover.

Trust rule (operator-confirmed): the verification material always comes from the
SAME source as the binary — the Terrapod cache when the binary came from the
cache, upstream when the runner fell back to upstream. We never ask the cache
for a signature it just failed to serve the binary for. Either way the signature
is checked against the pinned key shipped in this image, so neither source is a
trust anchor for authenticity.

Synchronous by design — the runner orchestrator is sync.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import structlog

from terrapod.gpg_verify import (
    load_key,
    load_key_from_armor,
    parse_sha256sums,
    verify_detached,
)
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.binary_verify")

_KEYS_DIR = Path(__file__).resolve().parent.parent / "upstream_keys"

_KEY_FILES = {
    "terraform": "hashicorp.asc",
    "tofu": "opentofu.asc",
    "terragrunt": "gruntwork.asc",
}


class ExecutableVerificationError(RuntimeError):
    """A downloaded executable failed integrity/signature verification.

    Fail-closed: the run must abort rather than execute an unverified binary.
    """


def _key_for_tool(tool: str):
    """Resolve the trusted publisher key: an operator override injected by
    job_template via TP_SIGNING_KEY_<TOOL> (from binary_cache.signing_keys), else
    the bundled pinned key. Mirrors the API's resolution so both honour the same
    operator-controlled trust set without an image rebuild."""
    override = os.environ.get(f"TP_SIGNING_KEY_{tool.upper()}")
    if override:
        return load_key_from_armor(override)
    return load_key(str(_KEYS_DIR / _KEY_FILES[tool]))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_name(tool: str, version: str, os_: str, arch: str) -> str:
    if tool == "terraform":
        return f"terraform_{version}_{os_}_{arch}.zip"
    if tool == "tofu":
        return f"tofu_{version}_{os_}_{arch}.zip"
    return f"terragrunt_{os_}_{arch}"  # bare per-platform binary


def _cache_sums_urls(cfg: RunnerConfig, tool: str, version: str) -> tuple[str, str]:
    base = f"{cfg.api_url}/api/terrapod/v1/binary-cache/{tool}/{version}"
    return f"{base}/sha256sums", f"{base}/sha256sums.sig"


def _upstream_sums_urls(tool: str, version: str) -> tuple[str, str]:
    if tool == "terraform":
        b = f"https://releases.hashicorp.com/terraform/{version}/terraform_{version}_SHA256SUMS"
        return b, f"{b}.sig"
    if tool == "tofu":
        b = f"https://github.com/opentofu/opentofu/releases/download/v{version}/tofu_{version}_SHA256SUMS"
        return b, f"{b}.gpgsig"
    b = f"https://github.com/gruntwork-io/terragrunt/releases/download/v{version}/SHA256SUMS"
    return b, f"{b}.gpgsig"


def _get(client: httpx.Client, url: str, headers: dict, retries: int, delay: float) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, headers=headers, follow_redirects=True, timeout=30.0)
            if resp.status_code == 200:
                return resp.content
            # 4xx is final; 5xx retries
            if resp.status_code < 500:
                raise ExecutableVerificationError(
                    f"could not fetch verification material from {url} (HTTP {resp.status_code})"
                )
            last = ExecutableVerificationError(f"HTTP {resp.status_code} from {url}")
        except httpx.HTTPError as exc:
            last = exc
        if attempt < retries:
            import time

            time.sleep(delay)
    raise ExecutableVerificationError(f"could not fetch {url}: {last}")


def verify_executable(
    cfg: RunnerConfig,
    tool: str,
    version: str,
    artifact_path: Path,
    *,
    from_cache: bool,
    client: httpx.Client,
    level: str | None = None,
) -> None:
    """Verify a downloaded executable artifact before it is run.

    ``artifact_path`` is the downloaded zip (terraform/tofu) or bare binary
    (terragrunt) — i.e. the file whose hash the SHA256SUMS manifest lists.
    Raises ``ExecutableVerificationError`` (fail-closed) on any failure. No-op
    when the effective level is ``off``.
    """
    level = level or cfg.verify_binaries
    if level == "off":
        logger.warning(
            "executable verification disabled (off) — running unverified binary", tool=tool
        )
        return
    if tool not in _KEY_FILES:
        raise ExecutableVerificationError(f"no pinned key for tool {tool!r}")

    if from_cache:
        sums_url, sig_url = _cache_sums_urls(cfg, tool, version)
        headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
        source = "Terrapod cache"
    else:
        sums_url, sig_url = _upstream_sums_urls(tool, version)
        headers = {}
        source = "upstream"

    manifest = _get(
        client, sums_url, headers, cfg.download_retries, cfg.download_retry_delay_seconds
    )

    key_uid = ""
    if level == "signature":
        sig = _get(client, sig_url, headers, cfg.download_retries, cfg.download_retry_delay_seconds)
        key = _key_for_tool(tool)
        if not verify_detached(manifest, sig, key):
            raise ExecutableVerificationError(
                f"GPG signature on {tool} SHA256SUMS ({source}) did not verify against the "
                f"pinned publisher key — refusing to run (possible tampering)"
            )
        key_uid = f"{key.fingerprint.keyid}"

    sums = parse_sha256sums(manifest.decode("utf-8", errors="replace"))
    artifact = _artifact_name(tool, version, cfg.os, cfg.arch)
    expected = sums.get(artifact)
    if expected is None:
        raise ExecutableVerificationError(
            f"{artifact} not listed in {tool} SHA256SUMS — cannot verify"
        )
    actual = _sha256_file(artifact_path)
    if expected.lower() != actual.lower():
        raise ExecutableVerificationError(
            f"checksum mismatch for {tool} {version} {cfg.os}/{cfg.arch}: "
            f"downloaded {actual}, manifest says {expected} — refusing to run (possible tampering)"
        )

    # Visible trust line in the run log (#607 "show off").
    if level == "signature":
        logger.info(
            f"✓ verified {tool} {version} ({cfg.os}/{cfg.arch}) — SHA-256 matches "
            f"signed manifest; signature valid (pinned key {key_uid}, via {source})"
        )
    else:
        logger.info(
            f"✓ verified {tool} {version} ({cfg.os}/{cfg.arch}) — SHA-256 matches "
            f"manifest (via {source})"
        )
