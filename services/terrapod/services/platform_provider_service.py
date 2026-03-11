"""Service layer for the built-in Terrapod platform provider distribution.

API ↔ Provider Contract:
    Every Terrapod instance serves its own Terraform provider via the standard
    registry protocol. When ``terraform init`` requests the ``terrapod`` provider
    from the instance's registry:

    1. Version list: returns the running platform version
    2. Download: check object-storage cache → if miss, fetch the matching
       binary from the GitHub Release → cache → return presigned URL

    Upstream URL pattern:
        https://github.com/mattrobinsonsre/terrapod/releases/download/v{version}/
            terraform-provider-terrapod_{version}_{os}_{arch}.zip

    Storage keys:
        cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_{os}_{arch}.zip
        cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS
        cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS.sig
"""

import hashlib
from functools import lru_cache
from pathlib import Path

import httpx

from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.storage.keys import (
    platform_provider_binary_key,
    platform_provider_shasums_key,
    platform_provider_shasums_sig_key,
)
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

GITHUB_RELEASE_BASE = "https://github.com/mattrobinsonsre/terrapod/releases/download"

VALID_OS = {"linux", "darwin"}
VALID_ARCH = {"amd64", "arm64"}

# Baked into the image at /etc/terrapod/signing-key.asc
_SIGNING_KEY_PATH = Path("/app/signing-key.asc")


@lru_cache(maxsize=1)
def _load_signing_key() -> list[dict]:
    """Load the provider signing public key from disk (cached)."""
    if not _SIGNING_KEY_PATH.exists():
        logger.warning("Provider signing key not found at %s", _SIGNING_KEY_PATH)
        return []
    ascii_armor = _SIGNING_KEY_PATH.read_text().strip()
    if not ascii_armor:
        return []
    # Extract key_id: last 16 hex chars of the fingerprint.
    # For simplicity, use pgpy if available, otherwise return without key_id.
    key_id = ""
    try:
        import pgpy

        key, _ = pgpy.PGPKey.from_blob(ascii_armor)
        key_id = str(key.fingerprint)[-16:].upper()
    except Exception:
        pass
    return [
        {
            "ascii_armor": ascii_armor,
            "key_id": key_id,
            "source": "terrapod",
            "source_url": "https://github.com/mattrobinsonsre/terrapod",
        }
    ]


def _release_url(version: str, filename: str) -> str:
    """Build the upstream GitHub Release URL for a provider artifact."""
    return f"{GITHUB_RELEASE_BASE}/v{version}/{filename}"


def get_platform_version() -> str:
    """Return the running platform version (without 'v' prefix).

    Falls back to '0.0.0-dev' if the version is not set in config.
    """
    version = getattr(settings, "version", None) or "0.0.1"
    return version.lstrip("v")


async def get_version_list() -> dict:
    """Return the provider version list in Terraform registry protocol format.

    Response shape matches GET /.../versions for a single-version provider.
    """
    version = get_platform_version()
    return {
        "versions": [
            {
                "version": version,
                "protocols": ["5.0", "6.0"],
                "platforms": [
                    {"os": os_, "arch": arch}
                    for os_ in sorted(VALID_OS)
                    for arch in sorted(VALID_ARCH)
                ],
            }
        ]
    }


async def get_download_info(
    storage: ObjectStore,
    version: str,
    os_: str,
    arch: str,
) -> dict:
    """Return download info for a specific platform binary.

    Fetches from upstream GitHub Release on cache miss, caches in object
    storage, and returns the standard Terraform download response with a
    presigned URL.

    Raises ValueError for invalid os/arch combinations.
    Raises RuntimeError if the upstream fetch fails.
    """
    if os_ not in VALID_OS:
        raise ValueError(f"Invalid OS: {os_}. Must be one of {VALID_OS}")
    if arch not in VALID_ARCH:
        raise ValueError(f"Invalid arch: {arch}. Must be one of {VALID_ARCH}")

    binary_key = platform_provider_binary_key(version, os_, arch)
    shasums_key = platform_provider_shasums_key(version)
    shasums_sig_key = platform_provider_shasums_sig_key(version)
    filename = f"terraform-provider-terrapod_{version}_{os_}_{arch}.zip"

    # Check if binary is already cached
    if await storage.exists(binary_key):
        logger.debug(
            "Platform provider cache hit",
            version=version,
            os=os_,
            arch=arch,
        )
    else:
        # Cache miss — fetch from GitHub Release
        logger.info(
            "Platform provider cache miss, fetching from GitHub",
            version=version,
            os=os_,
            arch=arch,
        )
        await _fetch_and_cache_binary(storage, version, filename, binary_key)

    # Ensure SHA256SUMS is cached (fetch once per version)
    if not await storage.exists(shasums_key):
        await _fetch_and_cache_shasums(storage, version, shasums_key)

    # Ensure SHA256SUMS.sig is cached (fetch once per version, optional)
    if not await storage.exists(shasums_sig_key):
        await _fetch_and_cache_shasums_sig(storage, version, shasums_sig_key)

    # Generate presigned URL for the binary
    presigned = await storage.presigned_get_url(binary_key)

    # Read SHA256SUMS to extract the shasum for this file
    shasum = await _get_shasum_for_file(storage, shasums_key, filename)

    return {
        "protocols": ["5.0", "6.0"],
        "os": os_,
        "arch": arch,
        "filename": filename,
        "download_url": presigned.url,
        "shasums_url": (await storage.presigned_get_url(shasums_key)).url,
        "shasums_signature_url": (await storage.presigned_get_url(shasums_sig_key)).url,
        "shasum": shasum,
        "signing_keys": {
            "gpg_public_keys": _load_signing_key(),
        },
    }


async def _fetch_and_cache_binary(
    storage: ObjectStore,
    version: str,
    filename: str,
    key: str,
) -> None:
    """Fetch a provider binary from GitHub and cache it."""
    url = _release_url(version, filename)
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as http:
        resp = await http.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch {url}: HTTP {resp.status_code}")
        data = resp.content

    sha256 = hashlib.sha256(data).hexdigest()
    await storage.put(key, data, content_type="application/zip")
    logger.info(
        "Cached platform provider binary",
        version=version,
        filename=filename,
        size=len(data),
        sha256=sha256,
    )


async def _fetch_and_cache_shasums(
    storage: ObjectStore,
    version: str,
    key: str,
) -> None:
    """Fetch SHA256SUMS from GitHub and cache it."""
    filename = f"terraform-provider-terrapod_{version}_SHA256SUMS"
    url = _release_url(version, filename)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        resp = await http.get(url)
        if resp.status_code != 200:
            logger.warning(
                "SHA256SUMS not available upstream",
                version=version,
                status=resp.status_code,
            )
            # Create an empty shasums file so we don't retry on every request
            await storage.put(key, b"", content_type="text/plain")
            return
        await storage.put(key, resp.content, content_type="text/plain")


async def _fetch_and_cache_shasums_sig(
    storage: ObjectStore,
    version: str,
    key: str,
) -> None:
    """Fetch SHA256SUMS.sig from GitHub and cache it (optional)."""
    filename = f"terraform-provider-terrapod_{version}_SHA256SUMS.sig"
    url = _release_url(version, filename)
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        resp = await http.get(url)
        if resp.status_code != 200:
            logger.debug(
                "SHA256SUMS.sig not available upstream",
                version=version,
                status=resp.status_code,
            )
            # Create empty so we don't retry
            await storage.put(key, b"", content_type="application/octet-stream")
            return
        await storage.put(key, resp.content, content_type="application/octet-stream")


async def _get_shasum_for_file(
    storage: ObjectStore,
    shasums_key: str,
    filename: str,
) -> str:
    """Extract the SHA256 hash for a specific file from the SHA256SUMS content."""
    data = await storage.get(shasums_key)
    if not data:
        return ""
    for line in data.decode("utf-8").strip().splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] == filename:
            return parts[0]
    return ""
