"""Tests for the registry provider service layer.

Focuses on the eager-h1 path at upload-confirm: when a runner-side
upload finalises a platform binary, we compute the terraform/tofu h1
dirhash from the just-uploaded bytes and persist it on the
`RegistryProviderPlatform` row, so the mirror's Tier-0 lookup can serve
it without a lazy backfill on the first read.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_provider_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("terraform-provider-terrapod_v0.33.0", b"binary contents")
    return buf.getvalue()


class TestUploadProviderBinaryEagerH1:
    """`upload_provider_binary` computes h1 from the uploaded bytes and
    persists it on the platform row, so the mirror's Tier-0 lookup
    doesn't have to lazy-backfill on the first download.
    """

    @pytest.mark.asyncio
    @patch(
        "terrapod.services.registry_provider_service.regenerate_shasums",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.services.registry_provider_service.upsert_provider_platform",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.services.registry_provider_service.upsert_provider_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.registry_provider_service.get_provider", new_callable=AsyncMock)
    async def test_h1_persisted_on_successful_upload(
        self,
        mock_get_provider: AsyncMock,
        mock_upsert_version: AsyncMock,
        mock_upsert_platform: AsyncMock,
        mock_regenerate_shasums: AsyncMock,
    ) -> None:
        from terrapod.services.registry_provider_service import upload_provider_binary

        provider = MagicMock()
        provider.id = "prov-id"
        mock_get_provider.return_value = provider

        prov_version = MagicMock()
        prov_version.id = "ver-id"
        mock_upsert_version.return_value = prov_version

        platform = MagicMock()
        platform.h1_hash = ""
        platform.shasum = ""
        platform.filename = ""
        platform.upload_status = "pending"
        mock_upsert_platform.return_value = platform

        db = AsyncMock()
        storage = AsyncMock()

        data = _make_provider_zip()

        await upload_provider_binary(
            db, storage, "default", "terrapod", "0.33.0", "linux", "amd64", data
        )

        # h1 was computed and stored without the `h1:` prefix.
        assert platform.h1_hash, "expected eager h1 compute to populate h1_hash"
        assert not platform.h1_hash.startswith("h1:"), "stored value omits the prefix"
        # And the rest of the upload flow ran.
        assert platform.upload_status == "uploaded"
        assert platform.shasum != ""
        storage.put.assert_awaited_once()
