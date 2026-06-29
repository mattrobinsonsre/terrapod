"""Supply-chain integrity verification for externally-fetched artifacts (#607).

Terrapod's pull-through caches fetch terraform/tofu/terragrunt binaries and
provider archives from upstream. Without verification, a compromised upstream,
a poisoned mirror, or a successful MITM would be cached and served (and, for
the CLI binaries, *executed* on every run). This module verifies what was
fetched before it is trusted:

- **Binaries**: fetch the publisher's ``SHA256SUMS`` manifest and (in
  ``signature`` mode) verify its detached GPG signature against the **pinned**
  publisher key shipped in this package (``upstream_keys/``), then check the
  downloaded artifact's SHA-256 against the entry in that signed manifest.
- **Providers**: compare the archive's SHA-256 against the registry-advertised
  shasum and (in ``signature`` mode) verify the registry's ``SHA256SUMS`` GPG
  signature against the registry-advertised signing key — mirroring what
  ``terraform init`` itself trusts.

All modes fail **closed**: on any verification failure the caller must reject
the fetch (cache nothing, serve nothing) and surface a clear error.

``pgpy`` signature verification is synchronous/CPU-bound, so it is dispatched
to a worker thread (HARD invariant: no sync work in an ``async`` handler).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
import pgpy
import structlog

from terrapod.config import settings
from terrapod.gpg_verify import (
    load_key,
    load_key_from_armor,
    parse_sha256sums,
    verify_detached,
)
from terrapod.http_retry import arequest_with_retry

logger = structlog.get_logger(__name__)

VerifyLevel = Literal["off", "checksum", "signature"]

_KEYS_DIR = Path(__file__).parent / "upstream_keys"


class VerificationError(Exception):
    """Raised when an externally-fetched artifact fails integrity verification.

    Fail-closed: the caller must not cache or serve the artifact.
    """


@dataclass(frozen=True)
class _BinarySpec:
    """How to locate and verify a tool's SHA256SUMS + signature upstream."""

    key_file: str  # pinned public key under upstream_keys/
    sums_path: str  # f-string with {base}/{version} → SHA256SUMS URL
    sig_path: str  # f-string with {base}/{version} → detached-sig URL
    artifact_name: str  # f-string with {version}/{os}/{arch} → manifest entry


# Upstream signing facts verified against live release artifacts (#607):
#   terraform  → HashiCorp key 34365D9472D7468F (RSA), releases.hashicorp.com
#   tofu       → OpenTofu key  0C0AF313E5FD9F80 (RSA), GitHub releases (.gpgsig)
#   terragrunt → Gruntwork key 577774ACA847CC49 (ed25519), GitHub releases (.gpgsig)
_BINARY_SPECS: dict[str, _BinarySpec] = {
    "terraform": _BinarySpec(
        key_file="hashicorp.asc",
        sums_path="{base}/{version}/terraform_{version}_SHA256SUMS",
        sig_path="{base}/{version}/terraform_{version}_SHA256SUMS.sig",
        artifact_name="terraform_{version}_{os}_{arch}.zip",
    ),
    "tofu": _BinarySpec(
        key_file="opentofu.asc",
        sums_path="{base}/v{version}/tofu_{version}_SHA256SUMS",
        sig_path="{base}/v{version}/tofu_{version}_SHA256SUMS.gpgsig",
        artifact_name="tofu_{version}_{os}_{arch}.zip",
    ),
    "terragrunt": _BinarySpec(
        key_file="gruntwork.asc",
        sums_path="{base}/v{version}/SHA256SUMS",
        sig_path="{base}/v{version}/SHA256SUMS.gpgsig",
        artifact_name="terragrunt_{os}_{arch}",
    ),
}


@lru_cache(maxsize=8)
def _load_key(key_file: str) -> pgpy.PGPKey:
    """Load a pinned public key from the bundled ``upstream_keys/`` dir.

    Cached for the process lifetime — the keys are static, image-baked assets.
    """
    return load_key(str(_KEYS_DIR / key_file))


def _key_for_tool(tool: str) -> pgpy.PGPKey:
    """Resolve the trusted publisher key for a tool: an operator-supplied
    override (``binary_cache.signing_keys[tool]``) if set, else the bundled
    pinned key. Lets operators rotate/replace keys without an image rebuild.
    """
    override = settings.registry.binary_cache.signing_keys.get(tool)
    if override:
        return load_key_from_armor(override)
    return _load_key(_BINARY_SPECS[tool].key_file)


# Cryptographic core lives in terrapod.gpg_verify (shared with the runner).
# `parse_sha256sums` is re-exported for callers/tests that import it here.
_verify_gpg_sync = verify_detached


async def _fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """Fetch a (small) manifest/signature with bounded retry; raise on non-200."""
    resp = await arequest_with_retry(client, "GET", url)
    if resp.status_code != 200:
        raise VerificationError(
            f"could not fetch verification material from {url} (HTTP {resp.status_code})"
        )
    return resp.content


async def fetch_sums_and_sig(
    client: httpx.AsyncClient,
    tool: str,
    version: str,
    *,
    level: VerifyLevel,
) -> tuple[bytes, bytes | None]:
    """Fetch the publisher SHA256SUMS (+ detached sig in ``signature`` mode)
    for a CLI tool/version and verify the signature against the pinned key.

    Returns ``(manifest_bytes, signature_bytes_or_None)``. Raises
    ``VerificationError`` on a bad/absent signature or unreachable material.
    Used both to verify a downloaded binary and to persist the signed manifest
    for the runner (which re-verifies it independently with the same pinned key).
    """
    spec = _BINARY_SPECS.get(tool)
    if spec is None:
        raise VerificationError(f"no verification spec for tool {tool!r}")

    cfg = settings.registry.binary_cache
    base = {
        "terraform": cfg.terraform_mirror_url,
        "tofu": cfg.tofu_mirror_url,
        "terragrunt": cfg.terragrunt_mirror_url,
    }[tool]

    sums_url = spec.sums_path.format(base=base, version=version)
    manifest = await _fetch_bytes(client, sums_url)

    signature: bytes | None = None
    if level == "signature":
        sig_url = spec.sig_path.format(base=base, version=version)
        signature = await _fetch_bytes(client, sig_url)
        key = _key_for_tool(tool)
        ok = await asyncio.to_thread(_verify_gpg_sync, manifest, signature, key)
        if not ok:
            raise VerificationError(
                f"GPG signature on {sums_url} did not verify against the pinned "
                f"{tool} publisher key — refusing to cache (possible tampering)"
            )
    return manifest, signature


async def verify_binary(
    client: httpx.AsyncClient,
    tool: str,
    version: str,
    os_: str,
    arch: str,
    artifact_sha256_hex: str,
    *,
    level: VerifyLevel,
) -> tuple[bytes, bytes | None]:
    """Verify a downloaded CLI binary against its published SHA256SUMS.

    Returns the ``(manifest, signature)`` it verified against so the caller can
    persist them for the runner. Raises ``VerificationError`` (fail-closed) on
    any mismatch, bad signature, missing manifest entry, or unreachable
    material. Returns ``(b"", None)`` when ``level == "off"``.
    """
    if level == "off":
        logger.warning(
            "binary verification disabled (verify=off) — trusting upstream bytes",
            tool=tool,
            version=version,
        )
        return b"", None

    spec = _BINARY_SPECS[tool] if tool in _BINARY_SPECS else None
    if spec is None:
        raise VerificationError(f"no verification spec for tool {tool!r}")

    manifest, signature = await fetch_sums_and_sig(client, tool, version, level=level)

    sums = parse_sha256sums(manifest.decode("utf-8", errors="replace"))
    artifact = spec.artifact_name.format(version=version, os=os_, arch=arch)
    expected = sums.get(artifact)
    if expected is None:
        raise VerificationError(f"{artifact} not listed in the SHA256SUMS manifest — cannot verify")
    if expected.lower() != artifact_sha256_hex.lower():
        raise VerificationError(
            f"checksum mismatch for {tool} {version} {os_}/{arch}: "
            f"downloaded {artifact_sha256_hex}, manifest says {expected} — "
            f"refusing to cache (possible tampering)"
        )
    return manifest, signature


async def verify_provider(
    client: httpx.AsyncClient,
    download_info: dict,
    archive_sha256_hex: str,
    *,
    level: VerifyLevel,
    allow_unsigned: bool = False,
) -> None:
    """Verify a downloaded provider archive against the registry download info.

    ``download_info`` is the upstream registry download response, which carries
    ``shasum`` and (for signature mode) ``shasums_url``, ``shasums_signature_url``
    and ``signing_keys.gpg_public_keys[].ascii_armor``. Raises
    ``VerificationError`` (fail-closed) on any failure. No-op when
    ``level == "off"``.

    ``allow_unsigned`` (opt-in, default off): in ``signature`` mode, when the
    upstream advertises NO signature material (private registries / non-signing
    mirrors / some community providers), degrade to the shasum check already
    performed above (with a warning) instead of rejecting. Off by default →
    strict fail-closed.
    """
    if level == "off":
        logger.warning("provider verification disabled (verify=off) — trusting upstream bytes")
        return

    advertised = (download_info.get("shasum") or "").lower()
    if not advertised:
        raise VerificationError("registry download response carried no shasum to verify against")
    if advertised != archive_sha256_hex.lower():
        raise VerificationError(
            f"provider archive checksum mismatch: downloaded {archive_sha256_hex}, "
            f"registry advertised {advertised} — refusing to cache (possible tampering)"
        )

    if level != "signature":
        return

    sums_url = download_info.get("shasums_url")
    sig_url = download_info.get("shasums_signature_url")
    signing_keys = (download_info.get("signing_keys") or {}).get("gpg_public_keys") or []
    if not sums_url or not sig_url or not signing_keys:
        if allow_unsigned:
            logger.warning(
                "provider upstream advertised no signature material — degrading to "
                "shasum-only verification (provider_cache.allow_unsigned=true). The "
                "archive checksum was verified against the advertised shasum."
            )
            return
        raise VerificationError(
            "registry download response lacked shasums/signature/signing keys; "
            "cannot perform signature verification (set provider_cache.verify=checksum, "
            "or provider_cache.allow_unsigned=true to degrade to shasum-only for "
            "unsigned upstreams)"
        )

    manifest = await _fetch_bytes(client, sums_url)
    signature = await _fetch_bytes(client, sig_url)

    # The advertised shasum must actually be present in the signed manifest,
    # otherwise a valid signature over an unrelated manifest would pass.
    if advertised not in {
        d.lower() for d in parse_sha256sums(manifest.decode("utf-8", "replace")).values()
    }:
        raise VerificationError(
            "registry-advertised shasum is not present in the signed SHA256SUMS manifest"
        )

    # Try each advertised key; accept on the first that verifies.
    for entry in signing_keys:
        armor = entry.get("ascii_armor")
        if not armor:
            continue
        try:
            key, _ = pgpy.PGPKey.from_blob(armor)
        except Exception:
            continue
        if await asyncio.to_thread(_verify_gpg_sync, manifest, signature, key):
            return
    raise VerificationError(
        "provider SHA256SUMS signature did not verify against any registry-advertised "
        "signing key — refusing to cache (possible tampering)"
    )


def _sha256_hex(data: bytes) -> str:
    """Convenience for tests/callers: hex SHA-256 of a byte string."""
    return hashlib.sha256(data).hexdigest()
