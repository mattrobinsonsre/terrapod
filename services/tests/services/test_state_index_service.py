"""The break-glass state index is current, and never lost to a bad read (#1581).

`state/index.yaml` lets an operator without PostgreSQL find each workspace's
latest state by name. It used to be written only by the CLI's local-mode upload
and a rename, so it was stale for every agent-mode workspace, and a transient
storage error while reading it wrote back a one-entry file that erased the rest.
"""

from __future__ import annotations

import pathlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from terrapod.services import state_index_service as sis
from terrapod.storage.keys import state_index_key, state_key
from terrapod.storage.protocol import ObjectNotFoundError

pytestmark = pytest.mark.asyncio

KEY = state_index_key()


class FakeStorage:
    def __init__(self, index: dict | None = None, *, get_error=None, put_error=None) -> None:
        self.objects: dict[str, bytes] = {}
        if index is not None:
            self.objects[KEY] = yaml.dump(index).encode()
        self.get_error = get_error
        self.put_error = put_error
        self.puts = 0

    async def get(self, key: str) -> bytes:
        if self.get_error is not None:
            raise self.get_error
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.objects[key] = data
        self.puts += 1

    def index(self) -> dict:
        return yaml.safe_load(self.objects[KEY]) or {}


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.acquired = 0

    async def set(self, key, value, nx=False, ex=None):  # noqa: ANN001
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.acquired += 1
        return True

    async def get(self, key):  # noqa: ANN001
        return self.values.get(key)

    async def delete(self, key):  # noqa: ANN001
        self.values.pop(key, None)


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        yield fake


def _use(storage: FakeStorage):
    return patch.object(sis, "get_storage", return_value=storage)


async def _record(name="app", ws_id=None, sv_id=None, serial=3) -> tuple[str, str]:
    ws_id = ws_id or str(uuid.uuid4())
    sv_id = sv_id or str(uuid.uuid4())
    await sis.record_latest_state(
        workspace_name=name, workspace_id=ws_id, state_version_id=sv_id, serial=serial
    )
    return ws_id, sv_id


class TestRecording:
    async def test_a_first_write_creates_the_index(self, redis) -> None:
        storage = FakeStorage()
        with _use(storage):
            ws_id, sv_id = await _record()
        entry = storage.index()["app"]
        assert entry["workspace_id"] == ws_id
        assert entry["state_key"] == state_key(ws_id, sv_id)
        assert entry["serial"] == 3
        assert entry["updated_at"].endswith("Z")

    async def test_other_workspaces_are_kept(self, redis) -> None:
        storage = FakeStorage({"other": {"workspace_id": "x", "state_key": "k", "serial": 1}})
        with _use(storage):
            await _record()
        assert set(storage.index()) == {"app", "other"}

    async def test_a_newer_serial_replaces_the_entry(self, redis) -> None:
        storage = FakeStorage()
        with _use(storage):
            ws_id, _ = await _record(serial=3)
            _, sv2 = await _record(ws_id=ws_id, serial=4)
        assert storage.index()["app"]["state_key"] == state_key(ws_id, sv2)

    async def test_a_slow_writer_cannot_roll_the_index_back(self, redis) -> None:
        storage = FakeStorage()
        with _use(storage):
            ws_id, sv1 = await _record(serial=5)
            await _record(ws_id=ws_id, serial=4)
        assert storage.index()["app"]["serial"] == 5
        assert storage.index()["app"]["state_key"] == state_key(ws_id, sv1)

    async def test_a_new_workspace_under_the_same_name_replaces_it(self, redis) -> None:
        """A restore is a new workspace id, usually under the old name; its
        serials may be lower than the entry it replaces and must still win."""
        storage = FakeStorage()
        with _use(storage):
            await _record(serial=9)
            new_ws, _ = await _record(serial=2)
        assert storage.index()["app"]["workspace_id"] == new_ws


class TestRemoving:
    async def test_the_entry_is_dropped(self, redis) -> None:
        storage = FakeStorage({"app": {"serial": 1}, "other": {"serial": 1}})
        with _use(storage):
            await sis.remove_workspace("app")
        assert set(storage.index()) == {"other"}

    async def test_an_absent_entry_writes_nothing(self, redis) -> None:
        storage = FakeStorage({"other": {"serial": 1}})
        with _use(storage):
            await sis.remove_workspace("app")
        assert storage.puts == 0

    async def test_no_index_at_all_writes_nothing(self, redis) -> None:
        storage = FakeStorage()
        with _use(storage):
            await sis.remove_workspace("app")
        assert KEY not in storage.objects


class TestNeverDestructive:
    async def test_a_transient_read_error_does_not_wipe_the_index(self, redis) -> None:
        """The old helper read any failure as "no index" and wrote back a
        one-entry file, erasing every other workspace."""
        storage = FakeStorage({"other": {"serial": 1}}, get_error=TimeoutError("storage timed out"))
        with _use(storage):
            await _record()
        assert storage.puts == 0
        assert yaml.safe_load(storage.objects[KEY]) == {"other": {"serial": 1}}

    async def test_a_corrupt_index_is_not_overwritten(self, redis) -> None:
        storage = FakeStorage()
        storage.objects[KEY] = b"- just\n- a list\n"
        with _use(storage):
            await _record()
        assert storage.puts == 0

    async def test_a_failed_write_never_reaches_the_caller(self, redis) -> None:
        storage = FakeStorage(put_error=RuntimeError("storage down"))
        with _use(storage):
            await _record()  # does not raise

    async def test_no_storage_at_all_never_reaches_the_caller(self, redis) -> None:
        with patch.object(sis, "get_storage", side_effect=RuntimeError("not initialised")):
            await _record()  # does not raise


class TestTheLock:
    async def test_the_lock_is_taken_and_released(self, redis) -> None:
        with _use(FakeStorage()):
            await _record()
        assert redis.acquired == 1
        assert sis.LOCK_KEY not in redis.values

    async def test_it_is_released_even_when_the_update_fails(self, redis) -> None:
        with _use(FakeStorage(put_error=RuntimeError("storage down"))):
            await _record()
        assert sis.LOCK_KEY not in redis.values

    async def test_no_redis_still_updates(self) -> None:
        storage = FakeStorage()
        with (
            patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("no redis")),
            _use(storage),
        ):
            await _record()
        assert "app" in storage.index()

    async def test_a_held_lock_is_waited_out_then_bypassed(self, redis) -> None:
        """Skipping would leave the entry stale for certain; updating unlocked
        risks it only on a rare race."""
        redis.values[sis.LOCK_KEY] = "someone-else"
        storage = FakeStorage()
        with (
            _use(storage),
            patch.object(sis, "LOCK_WAIT_SECONDS", 0.0),
            patch.object(sis.asyncio, "sleep", AsyncMock()),
        ):
            await _record()
        assert "app" in storage.index()
        # Another holder's lock is not released on its behalf.
        assert redis.values[sis.LOCK_KEY] == "someone-else"


class TestEveryStateWriteRecordsTheIndex:
    """Source-introspection invariant (#1581), in the style of #647's.

    Every module that constructs a `StateVersion` must also call
    `state_index_service.record_latest_state`, or the break-glass index goes
    stale for whatever that path writes. Before #1581 five of seven paths
    skipped it, including every agent-run apply.
    """

    ROOT = pathlib.Path(sis.__file__).resolve().parent.parent

    def _modules(self) -> list[pathlib.Path]:
        return sorted((self.ROOT / "api" / "routers").glob("*.py")) + sorted(
            (self.ROOT / "services").rglob("*.py")
        )

    def test_every_state_version_writer_records_the_index(self) -> None:
        offenders = [
            str(path.relative_to(self.ROOT))
            for path in self._modules()
            if "StateVersion(" in path.read_text()
            and "state_index_service.record_latest_state" not in path.read_text()
        ]
        assert offenders == [], (
            f"module(s) create a StateVersion without recording the state index: {offenders}"
        )

    def test_the_scan_finds_the_known_writers(self) -> None:
        """A guard that scans the wrong directory passes vacuously."""
        writers = {path.name for path in self._modules() if "StateVersion(" in path.read_text()}
        assert {
            "run_artifacts.py",
            "tfe_v2.py",
            "pulumi_service.py",
            "state_management.py",
            "deleted_workspace_service.py",
        } <= writers

    def test_there_is_one_index_writer(self) -> None:
        tfe_v2 = (self.ROOT / "api" / "routers" / "tfe_v2.py").read_text()
        assert "def _update_state_index" not in tfe_v2
        assert "def _remove_state_index_entry" not in tfe_v2
