"""Unit tests for the cloud-identity preflight doctor (terrapod.cli.preflight)."""

import pytest

from terrapod.cli import preflight as pf
from terrapod.config import StorageBackend


class _FakeStore:
    def __init__(self, raise_on_list: Exception | None = None) -> None:
        self._raise = raise_on_list

    async def list_prefix(self, prefix: str):
        if self._raise:
            raise self._raise
        return []


def _patch_storage(monkeypatch, store=None, init_exc=None):
    async def _init():
        if init_exc:
            raise init_exc

    async def _close():
        pass

    monkeypatch.setattr(pf, "init_storage", _init)
    monkeypatch.setattr(pf, "close_storage", _close)
    monkeypatch.setattr(pf, "get_storage", lambda: store)


def test_backend_cloud_mapping(monkeypatch):
    for backend, cloud in [
        (StorageBackend.S3, "aws"),
        (StorageBackend.GCS, "gcp"),
        (StorageBackend.AZURE, "azure"),
        (StorageBackend.FILESYSTEM, None),
    ]:
        monkeypatch.setattr(pf.settings.storage, "backend", backend)
        assert pf._backend_cloud() == cloud


@pytest.mark.asyncio
async def test_check_storage_passes(monkeypatch):
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.FILESYSTEM)
    _patch_storage(monkeypatch, store=_FakeStore())
    chk = await pf._check_storage()
    assert chk.ok is True


@pytest.mark.asyncio
async def test_check_storage_fails_on_list_error(monkeypatch):
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.S3)
    _patch_storage(monkeypatch, store=_FakeStore(raise_on_list=PermissionError("403 AccessDenied")))
    chk = await pf._check_storage()
    assert chk.ok is False
    assert "AccessDenied" in chk.detail


@pytest.mark.asyncio
async def test_check_storage_fails_on_init_error(monkeypatch):
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.S3)
    _patch_storage(monkeypatch, store=_FakeStore(), init_exc=RuntimeError("bad config"))
    chk = await pf._check_storage()
    assert chk.ok is False


def test_resolve_identity_filesystem_skips(monkeypatch):
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.FILESYSTEM)
    chk = pf._resolve_identity()
    assert chk.ok is None  # skipped, not a failure


@pytest.mark.asyncio
async def test_check_database_skipped_without_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    chk = await pf._check_database()
    assert chk.ok is None


@pytest.mark.asyncio
async def test_identity_mode_filesystem_passes(monkeypatch):
    # Runner identity probe on a filesystem backend: nothing to resolve → pass.
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.FILESYSTEM)
    monkeypatch.setenv("TP_PREFLIGHT_MODE", "identity")
    await pf.preflight()  # must not SystemExit


@pytest.mark.asyncio
async def test_full_mode_hard_fails_on_storage(monkeypatch):
    monkeypatch.setattr(pf.settings.storage, "backend", StorageBackend.S3)
    monkeypatch.setenv("TP_PREFLIGHT_MODE", "full")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _patch_storage(monkeypatch, store=_FakeStore(raise_on_list=PermissionError("403")))
    with pytest.raises(SystemExit) as exc:
        await pf.preflight()
    assert exc.value.code == 1
