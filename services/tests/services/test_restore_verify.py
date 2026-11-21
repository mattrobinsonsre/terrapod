"""Unit tests for the DR-drill CLI (terrapod.cli.restore_verify)."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

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
    assert await rv._latest_backup_key("backups/") == "backups/20260103T000000Z.dump"


@pytest.mark.asyncio
async def test_latest_backup_key_errors_when_empty(monkeypatch):
    store = _FakeStore(["backups/notes.txt"])
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    with pytest.raises(RuntimeError):
        await rv._latest_backup_key("backups/")


@pytest.mark.asyncio
async def test_verify_state_object_true_when_present(monkeypatch):
    store = _FakeStore(["state/ws/sv.tfstate"], data=b"{}")
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    assert await rv._verify_state_objects() == (["default"], [])
    assert store.got == ["state/ws/sv.tfstate"]


@pytest.mark.asyncio
async def test_verify_state_object_false_when_absent(monkeypatch):
    store = _FakeStore([])
    monkeypatch.setattr(rv, "get_storage", lambda: store)
    assert await rv._verify_state_objects() == ([], [])


class _UnreadableStore(_FakeStore):
    async def get(self, key: str) -> bytes:
        raise PermissionError(f"AccessDenied: {key}")


@pytest.mark.asyncio
async def test_every_store_is_read_and_an_unreadable_one_fails_the_drill(monkeypatch):
    from terrapod.storage.routing import FanOutResolver, RoutingStore

    home = _FakeStore(["state/a/1.tfstate"])
    eu = _UnreadableStore(["state/b/1.tfstate"])
    us = _FakeStore([])
    router = RoutingStore(home, {"eu-west": eu, "us-east": us}, FanOutResolver())
    monkeypatch.setattr(rv, "get_storage", lambda: router)

    read, failures = await rv._verify_state_objects()

    assert read == ["default"]
    assert len(failures) == 1 and failures[0].startswith("eu-west:")
    assert "AccessDenied" in failures[0]


@pytest.mark.asyncio
async def test_an_unreadable_location_is_a_drill_failure(monkeypatch):
    from terrapod.storage.routing import FanOutResolver, RoutingStore

    router = RoutingStore(
        _FakeStore(["state/a/1.tfstate"]),
        {"eu-west": _UnreadableStore(["state/b/1.tfstate"])},
        FanOutResolver(),
    )
    monkeypatch.setattr(rv, "get_storage", lambda: router)
    conn = AsyncMock()
    conn.execute.return_value = MagicMock(scalar_one=MagicMock(return_value=1))
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = conn
    engine.dispose = AsyncMock()
    monkeypatch.setattr(rv, "create_async_engine", lambda _dsn: engine)

    failures = await rv._verify_invariants("postgresql+asyncpg://scratch/db")

    assert any(f.startswith("eu-west:") for f in failures)


class TestTheBackupPrefixIsRefusedEarly:
    """`restore_verify` must refuse a bad `backup.prefix` the way `backup` does.

    It used to resolve it inside `_latest_backup_key`, which is an untested call
    site three ways over (#2621): every test drove the helper directly, the call is
    SKIPPED entirely when `TP_RESTORE_KEY` names a key, and it runs after
    `init_storage()` and `mkstemp` -- so the refusal was late, skippable, and
    arrived as an uncaught `ValueError` traceback rather than the one-line error
    the operator doc promises of both jobs.
    """

    @pytest.fixture(autouse=True)
    def _target(self, monkeypatch):
        monkeypatch.setenv("TP_RESTORE_TARGET_URL", "postgresql://u@h:5432/throwaway")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("TP_RESTORE_KEY", raising=False)

    @pytest.fixture
    def never_starts(self, monkeypatch):
        """Anything past the refusal must not run."""
        reached = MagicMock()
        monkeypatch.setattr(rv, "init_storage", AsyncMock(side_effect=reached))
        return reached

    @pytest.mark.parametrize("prefix", ["state/", "vcs_archives/", "", "./"])
    async def test_a_bad_prefix_exits_one_before_storage_is_touched(
        self, monkeypatch, never_starts, prefix
    ):
        monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": prefix})())
        with pytest.raises(SystemExit) as exc:
            await rv.restore_verify()
        assert exc.value.code == 1
        never_starts.assert_not_called()

    async def test_it_is_refused_even_when_the_key_is_named(self, monkeypatch, never_starts):
        """The branch that reads the latest key is skipped; the refusal is not."""
        monkeypatch.setenv("TP_RESTORE_KEY", "backups/20260101T000000Z.dump")
        monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": "state/"})())
        with pytest.raises(SystemExit) as exc:
            await rv.restore_verify()
        assert exc.value.code == 1
        never_starts.assert_not_called()

    async def test_the_refusal_is_one_line_and_names_the_setting(self, monkeypatch, never_starts):
        """Not a traceback: an uncaught ValueError is what this replaced."""
        log = MagicMock()
        monkeypatch.setattr(rv, "logger", log)
        monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": "state/"})())
        with pytest.raises(SystemExit):
            await rv.restore_verify()
        assert log.error.call_count == 1
        assert "backup.prefix" in str(log.error.call_args)

    async def test_the_normalised_prefix_is_what_reaches_the_listing(self, monkeypatch):
        """`./backups/` must list `backups/`, or the drill reports no backups found
        on a store that holds them."""
        store = _FakeStore(["backups/20260101T000000Z.dump"])
        monkeypatch.setattr(rv, "get_storage", lambda: store)
        monkeypatch.setattr(rv.settings, "backup", type("B", (), {"prefix": "./backups/"})())

        seen: list[str] = []
        original = store.list_prefix

        async def record(prefix, **kw):
            seen.append(prefix)
            return await original(prefix, **kw)

        store.list_prefix = record
        from terrapod.storage.keys import shared_prefix

        prefix = shared_prefix("backup.prefix", rv.settings.backup.prefix)
        assert await rv._latest_backup_key(prefix) == "backups/20260101T000000Z.dump"
        assert seen == ["backups/"]
