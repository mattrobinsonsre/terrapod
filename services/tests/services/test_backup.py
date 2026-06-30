"""Unit tests for the logical-backup CLI (terrapod.cli.backup)."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from terrapod.cli import backup as bk


@dataclass
class _Meta:
    key: str


class _FakeStore:
    """Minimal ObjectStore stand-in for prune tests."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)
        self.deleted: list[str] = []

    async def list_prefix(self, prefix: str) -> list[_Meta]:
        return [_Meta(k) for k in self._keys if k.startswith(prefix)]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


def test_libpq_dsn_strips_asyncpg():
    assert bk._libpq_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    # Plain libpq URL is unchanged.
    assert bk._libpq_dsn("postgresql://u@h/db") == "postgresql://u@h/db"


def test_backup_ts_is_sortable_and_parseable():
    ts = bk._backup_ts()
    # round-trips through the parser used by retention
    epoch = bk._parse_key_ts(f"backups/{ts}.dump", "backups/")
    assert epoch is not None
    # well-formed: parseable as the documented format
    datetime.strptime(ts, "%Y%m%dT%H%M%SZ")


def test_parse_key_ts_handles_bad_names():
    assert bk._parse_key_ts("backups/not-a-timestamp.dump", "backups/") is None
    good = bk._parse_key_ts("backups/20260101T000000Z.dump", "backups/")
    assert good == datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def test_pg_dump_env_password_mode_adds_no_pgpassword(monkeypatch):
    monkeypatch.delenv("TP_DB_AUTH_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    env = bk._pg_dump_env()
    assert "PGPASSWORD" not in env


@pytest.mark.asyncio
async def test_prune_keep_count(monkeypatch):
    keys = [f"backups/2026010{i}T000000Z.dump" for i in range(1, 6)]  # 5 backups
    store = _FakeStore(keys)
    monkeypatch.setattr(bk, "get_storage", lambda: store)
    await bk._prune("backups/", keep=2, days=0)
    # keeps the 2 newest (…05, …04); deletes the 3 oldest
    assert sorted(store.deleted) == sorted(keys[:3])


@pytest.mark.asyncio
async def test_prune_by_age(monkeypatch):
    old = "backups/20000101T000000Z.dump"
    new = f"backups/{bk._backup_ts()}.dump"
    store = _FakeStore([old, new])
    monkeypatch.setattr(bk, "get_storage", lambda: store)
    await bk._prune("backups/", keep=0, days=30)
    assert store.deleted == [old]


@pytest.mark.asyncio
async def test_prune_noop_when_disabled(monkeypatch):
    store = _FakeStore(["backups/20260101T000000Z.dump"])
    monkeypatch.setattr(bk, "get_storage", lambda: store)
    await bk._prune("backups/", keep=0, days=0)
    assert store.deleted == []


@pytest.mark.asyncio
async def test_prune_ignores_non_dump_keys(monkeypatch):
    store = _FakeStore(["backups/keep.txt", "backups/20260101T000000Z.dump"])
    monkeypatch.setattr(bk, "get_storage", lambda: store)
    await bk._prune("backups/", keep=1, days=0)
    assert store.deleted == []  # only one .dump, kept


@pytest.mark.asyncio
async def test_backup_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        await bk.backup()
