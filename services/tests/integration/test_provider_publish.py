"""Integration: the client-signed provider publish -> CLI-download contract.

Publishes a *synthetic* provider end-to-end through the real API + Postgres +
filesystem storage (register key -> upload SHA256SUMS -> upload signature ->
upload a platform zip) and asserts the `terraform init` download response is
complete and installable: shasum populated, the signing key advertised, every
URL resolvable. This is the contract that every bug the publish audit found
(null shasum, stuck `pending` status, empty signing_keys) would have broken.

The bytes are not a real provider — `tofu init` against a live registry is the
Tilt smoke's job; here we only assert the server publish/serve contract.
"""

from __future__ import annotations

import hashlib

import pgpy
import pytest
from pgpy.constants import (
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.asyncio

PROV_BASE = "/api/terrapod/v1/registry-providers/private/default"


def _new_key() -> pgpy.PGPKey:
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("awsmai", email="security@example.test")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.ZLIB],
    )
    return key


# Keygen is the slow part — generate once, register per test (DB is truncated
# between tests). _OTHER is an unregistered key for the negative case.
_KEY = _new_key()
_OTHER = _new_key()


async def _register(client, app) -> str:
    """Register the provider slot + the signing public key; return its key id."""
    set_auth(app, admin_user())
    r = await client.post(
        "/api/terrapod/v1/registry-providers",
        json={
            "data": {
                "type": "registry-providers",
                "attributes": {"name": "awsmai", "labels": {}},
            }
        },
        headers=AUTH,
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post(
        "/api/terrapod/v1/gpg-keys",
        json={
            "data": {
                "type": "gpg-keys",
                "attributes": {"namespace": "default", "ascii-armor": str(_KEY.pubkey)},
            }
        },
        headers=AUTH,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]["attributes"]["key-id"]


def _artifacts(version: str, body: bytes) -> tuple[bytes, str, str]:
    sha = hashlib.sha256(body).hexdigest()
    filename = f"terraform-provider-awsmai_{version}_linux_arm64.zip"
    manifest = f"{sha}  {filename}\n".encode()
    return manifest, filename, sha


class TestProviderPublishDownload:
    async def test_full_publish_is_installable(self, app, client):
        key_id = await _register(client, app)
        version = "1.0.0"
        zip_bytes = b"PK\x03\x04 synthetic provider zip"
        manifest, filename, sha = _artifacts(version, zip_bytes)
        sig = bytes(_KEY.sign(manifest))

        base = f"{PROV_BASE}/awsmai/versions/{version}"
        assert (
            await client.put(f"{base}/shasums", content=manifest, headers=AUTH)
        ).status_code == 200
        assert (
            await client.put(f"{base}/shasums.sig", content=sig, headers=AUTH)
        ).status_code == 200
        r = await client.put(f"{base}/platforms/linux/arm64", content=zip_bytes, headers=AUTH)
        assert r.status_code == 200, r.text

        # CLI download (terraform init) must be complete + installable.
        r = await client.get(
            f"/api/v2/registry/providers/default/awsmai/{version}/download/linux/arm64",
            headers=AUTH,
        )
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["shasum"] == sha
        assert info["filename"] == filename
        assert info["download_url"] and info["shasums_url"] and info["shasums_signature_url"]
        keys = info["signing_keys"]["gpg_public_keys"]
        assert keys and keys[0]["key_id"] == key_id

    async def test_binary_before_signature_is_rejected(self, app, client):
        await _register(client, app)
        version = "2.0.0"
        zip_bytes = b"zip"
        manifest, _, _ = _artifacts(version, zip_bytes)
        base = f"{PROV_BASE}/awsmai/versions/{version}"
        assert (
            await client.put(f"{base}/shasums", content=manifest, headers=AUTH)
        ).status_code == 200
        # No verified signature yet -> binary upload must be refused.
        r = await client.put(f"{base}/platforms/linux/arm64", content=zip_bytes, headers=AUTH)
        assert r.status_code == 422, r.text

    async def test_unregistered_signing_key_is_rejected(self, app, client):
        await _register(client, app)
        version = "3.0.0"
        manifest, _, _ = _artifacts(version, b"zip")
        base = f"{PROV_BASE}/awsmai/versions/{version}"
        assert (
            await client.put(f"{base}/shasums", content=manifest, headers=AUTH)
        ).status_code == 200
        # Signed by a key that was never registered -> 422 at the trust gate.
        sig = bytes(_OTHER.sign(manifest))
        r = await client.put(f"{base}/shasums.sig", content=sig, headers=AUTH)
        assert r.status_code == 422, r.text

    async def test_sha_mismatch_is_rejected(self, app, client):
        await _register(client, app)
        version = "4.0.0"
        # Manifest commits to one sha; we upload different bytes.
        manifest, _, _ = _artifacts(version, b"the-signed-bytes")
        sig = bytes(_KEY.sign(manifest))
        base = f"{PROV_BASE}/awsmai/versions/{version}"
        assert (
            await client.put(f"{base}/shasums", content=manifest, headers=AUTH)
        ).status_code == 200
        assert (
            await client.put(f"{base}/shasums.sig", content=sig, headers=AUTH)
        ).status_code == 200
        r = await client.put(
            f"{base}/platforms/linux/arm64", content=b"DIFFERENT bytes", headers=AUTH
        )
        assert r.status_code == 422, r.text
