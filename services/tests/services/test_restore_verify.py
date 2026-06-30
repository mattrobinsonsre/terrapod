"""Unit tests for the DR-drill CLI (terrapod.cli.restore_verify)."""

from dataclasses import dataclass

import pytest

from terrapod.cli import restore_verify as rv


@dataclass
class _Meta:
    key: str


class _FakeStore:
    def __init__(self, keys: list[str], data: bytes = b"x") -> None:
        self._keys = list(keys)
        self._data = data
        self.got: list[str] = []

    async def list_prefix(self, prefix: str) -> list[_Meta]:
        return [_Meta(k) for k in self._keys if k.startswith(prefix)]

    async def get(self, key: str) -> bytes:
        self.got.append(key)
        return self._data


def test_dsn_helpers():
    assert rv._libpq_dsn("postgresql+asyncpg://u@h/db") == "postgresql://u@h/db"
    assert rv._async_dsn("postgresql://u@h/db") == "postgresql+asyncpg://u@h/db"
    # already-async is left alone
    assert rv._async_dsn("postgresql+asyncpg://u@h/db") == "postgresql+asyncpg://u@h/db"


@pytest.mark.asyncio
async def test_refuses_when_target_equals_live(monkeypatch):
    monkeypatch.setenv("TP_RESTORE_TARGET_URL", "postgresql://u@h:5432/db")
    # Same DB, different driver suffix — must still be detected as equal.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h:5432/db")
    with pytest.raises(SystemExit) as exc:
        await rv.restore_verify()
    assert exc.value.code == 1


@pytest.mark.asyncio
async def test_requires_target(monkeypatch):
    monkeypatch.delenv("TP_RESTORE_TARGET_URL", raising=False)
    with pytest.raises(SystemExit):
        await rv.restore_verify()


@pytest.mark.asyncio
async def test_latest_backup_key_picks_newest(monkeypatch):
    keys = [
        "backups/20260101T000000Z.dump",
        "backups/20260103T000000Z.dump",
        "backups/20260102T000000Z.dump",
        "backups/notes.txt",
    ]
    store = _FakeStore(keys)
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": "backups/"})())
    assert await rv._latest_backup_key() == "backups/20260103T000000Z.dump"


@pytest.mark.asyncio
async def test_latest_backup_key_errors_when_empty(monkeypatch):
    store = _FakeStore(["backups/notes.txt"])
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": "backups/"})())
    with pytest.raises(RuntimeError):
        await rv._latest_backup_key()


@pytest.mark.asyncio
async def test_verify_state_object_true_when_present(monkeypatch):
    store = _FakeStore(["state/ws/sv.tfstate"], data=b"{}")
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    assert await rv._verify_state_object() is True
    assert store.got == ["state/ws/sv.tfstate"]


@pytest.mark.asyncio
async def test_verify_state_object_false_when_absent(monkeypatch):
    store = _FakeStore([])
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    assert await rv._verify_state_object() is False
