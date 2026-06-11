"""Tests for the client-signed provider publish path in the registry service.

The publish protocol is: upload SHA256SUMS, then its detached signature
(verified against a *registered* GPG key — the trust gate), then each
platform zip (validated against the signed manifest as it streams in). The
server never re-signs. These tests exercise the three service primitives
(`store_provider_shasums`, `store_and_verify_provider_sig`,
`record_provider_binary`) with a real pgpy keypair so the signature
verification is genuine, not mocked.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pgpy
import pytest
from pgpy.constants import (
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from terrapod.services.registry_provider_service import (
    PublishValidationError,
    record_provider_binary,
    store_and_verify_provider_sig,
    store_provider_shasums,
)

# One RSA keypair for the whole module — keygen is the slow part (~1-3s).
_KEY = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
_UID = pgpy.PGPUID.new("awsmai test", email="security@example.test")
_KEY.add_uid(
    _UID,
    usage={KeyFlags.Sign},
    hashes=[HashAlgorithm.SHA256],
    ciphers=[SymmetricKeyAlgorithm.AES256],
    compression=[CompressionAlgorithm.ZLIB],
)
_PUB_ARMOR = str(_KEY.pubkey)
_KEY_ID = _KEY.fingerprint.replace(" ", "")[-16:].upper()


def _detached_sig(data: bytes) -> bytes:
    """A detached binary signature over raw bytes — exactly what gpg
    --detach-sign / the Go client produce, and what tofu verifies."""
    return bytes(_KEY.sign(data))


def _registered_key() -> MagicMock:
    gpg = MagicMock()
    gpg.id = "gpgkey-uuid"
    gpg.key_id = _KEY_ID
    gpg.ascii_armor = _PUB_ARMOR
    return gpg


def _manifest(filename: str, sha: str) -> bytes:
    return f"{sha}  {filename}\n".encode()


class TestStoreAndVerifyProviderSig:
    """The signature upload is the trust gate: it must verify against a
    *registered* key over the *uploaded* manifest, or 422."""

    @pytest.mark.asyncio
    @patch("terrapod.services.gpg_key_service.get_gpg_key_by_key_id", new_callable=AsyncMock)
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_valid_signature_links_key(
        self,
        mock_get_provider: AsyncMock,
        mock_get_version: AsyncMock,
        mock_get_key: AsyncMock,
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        version = MagicMock(gpg_key_id=None, shasums_sig_uploaded=False)
        mock_get_version.return_value = version
        mock_get_key.return_value = _registered_key()

        manifest = _manifest("terraform-provider-awsmai_1.0.0_linux_arm64.zip", "a" * 64)
        sig = _detached_sig(manifest)

        storage = AsyncMock()
        storage.get.return_value = manifest

        result = await store_and_verify_provider_sig(
            AsyncMock(), storage, "default", "awsmai", "1.0.0", sig
        )

        assert result.shasums_sig_uploaded is True
        assert result.gpg_key_id == "gpgkey-uuid"
        storage.put.assert_awaited_once()  # signature persisted

    @pytest.mark.asyncio
    @patch("terrapod.services.gpg_key_service.get_gpg_key_by_key_id", new_callable=AsyncMock)
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_tampered_manifest_rejected(
        self,
        mock_get_provider: AsyncMock,
        mock_get_version: AsyncMock,
        mock_get_key: AsyncMock,
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(gpg_key_id=None, shasums_sig_uploaded=False)
        mock_get_key.return_value = _registered_key()

        signed = _manifest("file.zip", "a" * 64)
        sig = _detached_sig(signed)
        storage = AsyncMock()
        storage.get.return_value = signed + b"tampered\n"  # server reads a DIFFERENT manifest

        with pytest.raises(PublishValidationError):
            await store_and_verify_provider_sig(
                AsyncMock(), storage, "default", "awsmai", "1.0.0", sig
            )

    @pytest.mark.asyncio
    @patch("terrapod.services.gpg_key_service.get_gpg_key_by_key_id", new_callable=AsyncMock)
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_unregistered_key_rejected(
        self,
        mock_get_provider: AsyncMock,
        mock_get_version: AsyncMock,
        mock_get_key: AsyncMock,
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(gpg_key_id=None, shasums_sig_uploaded=False)
        mock_get_key.return_value = None  # key not registered

        manifest = _manifest("file.zip", "a" * 64)
        storage = AsyncMock()
        storage.get.return_value = manifest

        with pytest.raises(PublishValidationError):
            await store_and_verify_provider_sig(
                AsyncMock(), storage, "default", "awsmai", "1.0.0", _detached_sig(manifest)
            )

    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_signature_before_manifest_rejected(
        self, mock_get_provider: AsyncMock, mock_get_version: AsyncMock
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(gpg_key_id=None, shasums_sig_uploaded=False)
        storage = AsyncMock()
        storage.get.side_effect = FileNotFoundError("no manifest")

        with pytest.raises(PublishValidationError):
            await store_and_verify_provider_sig(
                AsyncMock(), storage, "default", "awsmai", "1.0.0", b"sig"
            )


class TestRecordProviderBinary:
    """Binary uploads are gated on a verified manifest and validated
    against it byte-for-byte before storage."""

    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.upsert_provider_platform",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_matching_sha_stored(
        self,
        mock_get_provider: AsyncMock,
        mock_get_version: AsyncMock,
        mock_upsert_platform: AsyncMock,
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(shasums_sig_uploaded=True)
        platform = MagicMock(shasum="", filename="", upload_status="pending")
        mock_upsert_platform.return_value = platform

        body = b"zip-bytes"
        sha = hashlib.sha256(body).hexdigest()
        filename = "terraform-provider-awsmai_1.0.0_linux_arm64.zip"
        storage = AsyncMock()
        storage.get.return_value = _manifest(filename, sha)

        fd, tmp = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            result = await record_provider_binary(
                AsyncMock(),
                storage,
                "default",
                "awsmai",
                "1.0.0",
                "linux",
                "arm64",
                sha256=sha,
                filename=filename,
                tmp_path=tmp,
            )
        finally:
            os.unlink(tmp)

        assert result.upload_status == "uploaded"
        assert result.shasum == sha
        storage.put_stream.assert_awaited_once()

    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_sha_mismatch_rejected(
        self, mock_get_provider: AsyncMock, mock_get_version: AsyncMock
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(shasums_sig_uploaded=True)
        filename = "terraform-provider-awsmai_1.0.0_linux_arm64.zip"
        storage = AsyncMock()
        storage.get.return_value = _manifest(filename, "b" * 64)  # signed sha differs

        with pytest.raises(PublishValidationError):
            await record_provider_binary(
                AsyncMock(),
                storage,
                "default",
                "awsmai",
                "1.0.0",
                "linux",
                "arch",
                sha256="a" * 64,
                filename=filename,
                tmp_path="/dev/null",
            )

    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.get_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_binary_before_signature_rejected(
        self, mock_get_provider: AsyncMock, mock_get_version: AsyncMock
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        mock_get_version.return_value = MagicMock(shasums_sig_uploaded=False)  # not gated yet

        with pytest.raises(PublishValidationError):
            await record_provider_binary(
                AsyncMock(),
                AsyncMock(),
                "default",
                "awsmai",
                "1.0.0",
                "linux",
                "arm64",
                sha256="a" * 64,
                filename="x.zip",
                tmp_path="/dev/null",
            )


class TestStoreProviderShasums:
    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.upsert_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_manifest_stored_and_flagged(
        self, mock_get_provider: AsyncMock, mock_upsert_version: AsyncMock
    ) -> None:
        mock_get_provider.return_value = MagicMock(id="prov")
        version = MagicMock(shasums_uploaded=False)
        mock_upsert_version.return_value = version
        storage = AsyncMock()

        result = await store_provider_shasums(
            AsyncMock(), storage, "default", "awsmai", "1.0.0", b"sha  file.zip\n"
        )

        assert result.shasums_uploaded is True
        storage.put.assert_awaited_once()
