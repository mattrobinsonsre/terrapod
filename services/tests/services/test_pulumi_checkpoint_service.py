"""Local-mode Pulumi state: one state version per update (#1564).

A checkpoint is held against its update, and the last one becomes a single,
complete state version when the update ends. The #1581 checks that the write
records the break-glass index moved here with the write itself.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import pulumi_checkpoint_service as svc
from terrapod.storage.keys import pulumi_checkpoint_key, state_key
from terrapod.storage.protocol import ObjectNotFoundError

pytestmark = pytest.mark.asyncio

DEPLOYMENT = {"manifest": {"time": "2026-09-14T00:00:00Z"}, "resources": [{"urn": "a"}]}


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::dev"
    return ws


def _db(latest_serial: int | None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    if latest_serial is None:
        result.scalar_one_or_none.return_value = None
    else:
        latest = MagicMock()
        latest.serial = latest_serial
        result.scalar_one_or_none.return_value = latest
    db.execute.return_value = result
    db.add = MagicMock()
    return db


class _Store:
    """An in-memory object store, enough of one for these paths."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = False
        self.fail_delete = False

    async def put(self, key: str, data: bytes, **_: object) -> None:
        if self.fail_put:
            raise RuntimeError("storage down")
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("delete refused")
        self.objects.pop(key, None)


def _patches(store: _Store, *, record: AsyncMock, discard: AsyncMock) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("terrapod.storage.get_storage", return_value=store))
    stack.enter_context(
        patch("terrapod.crypto.state.encrypt_state_bytes", AsyncMock(side_effect=lambda b: b))
    )
    stack.enter_context(
        patch("terrapod.crypto.state.decrypt_state_bytes", AsyncMock(side_effect=lambda b: b))
    )
    stack.enter_context(
        patch("terrapod.services.run_service.discard_stale_plans_for_state_change", discard)
    )
    stack.enter_context(patch("terrapod.services.state_index_service.record_latest_state", record))
    return stack


class _Harness:
    def __init__(self) -> None:
        self.store = _Store()
        self.record = AsyncMock()
        self.discard = AsyncMock()

    def __enter__(self) -> _Harness:
        self._stack = _patches(self.store, record=self.record, discard=self.discard)
        self._stack.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.__exit__(*exc)


class TestAStateVersionIsComplete:
    """What a Terraform state version carries, a Pulumi one now carries too."""

    async def test_it_carries_its_size_hashes_and_creator(self) -> None:
        ws, db = _ws(), _db(latest_serial=1)
        with _Harness() as h:
            sv = await svc.write_deployment(db, ws, DEPLOYMENT, created_by="a@b.c")
        payload = json.dumps(DEPLOYMENT).encode()
        assert sv.serial == 2
        # Non-zero, which is what lets the delete guard protect the current version.
        assert sv.state_size == len(payload) > 0
        assert sv.md5 == hashlib.md5(payload).hexdigest()  # noqa: S324
        assert sv.sha256 == hashlib.sha256(payload).hexdigest()
        assert sv.created_by == "a@b.c"
        assert h.store.objects[state_key(str(ws.id), str(sv.id))] == payload

    async def test_the_id_is_time_ordered(self) -> None:
        """A restore numbers Pulumi versions by key order, so it must be age."""
        with _Harness():
            sv = await svc.write_deployment(_db(None), _ws(), DEPLOYMENT, created_by=None)
        assert sv.id.version == 7

    async def test_the_index_names_it(self) -> None:
        ws = _ws()
        with _Harness() as h:
            sv = await svc.write_deployment(_db(1), ws, DEPLOYMENT, created_by=None)
        h.record.assert_awaited_once_with(
            workspace_name="proj::dev", workspace_id=ws.id, state_version_id=sv.id, serial=2
        )

    async def test_the_first_write_is_serial_one(self) -> None:
        with _Harness() as h:
            await svc.write_deployment(_db(None), _ws(), DEPLOYMENT, created_by=None)
        assert h.record.await_args.kwargs["serial"] == 1

    async def test_stale_plans_are_discarded(self) -> None:
        ws, db = _ws(), _db(4)
        with _Harness() as h:
            await svc.write_deployment(db, ws, DEPLOYMENT, created_by=None)
        h.discard.assert_awaited_once_with(db, ws.id, 5)

    async def test_nothing_is_recorded_when_the_state_never_landed(self) -> None:
        """The index must never name an object that was not written."""
        db = _db(1)
        with _Harness() as h:
            h.store.fail_put = True
            with pytest.raises(RuntimeError):
                await svc.write_deployment(db, _ws(), DEPLOYMENT, created_by=None)
        h.record.assert_not_awaited()
        db.commit.assert_not_awaited()


class TestACheckpointIsHeld:
    async def test_it_writes_no_state_version(self) -> None:
        ws = _ws()
        with _Harness() as h:
            await svc.hold_checkpoint(ws.id, "u-1", DEPLOYMENT, created_by="a@b.c")
        key = pulumi_checkpoint_key(str(ws.id), "u-1")
        assert list(h.store.objects) == [key]
        assert json.loads(h.store.objects[key]) == {
            "created_by": "a@b.c",
            "deployment": DEPLOYMENT,
        }
        h.record.assert_not_awaited()

    async def test_a_later_checkpoint_replaces_the_one_before(self) -> None:
        ws = _ws()
        with _Harness() as h:
            await svc.hold_checkpoint(ws.id, "u-1", {"manifest": {"n": 1}}, created_by=None)
            await svc.hold_checkpoint(ws.id, "u-1", {"manifest": {"n": 2}}, created_by=None)
        assert len(h.store.objects) == 1
        (held,) = h.store.objects.values()
        assert json.loads(held)["deployment"] == {"manifest": {"n": 2}}

    def test_it_is_kept_away_from_the_workspaces_state_versions(self) -> None:
        """Restore reads every object under `state/{id}/` as a state version."""
        ws_id = str(uuid.uuid4())
        key = pulumi_checkpoint_key(ws_id, "u-1")
        assert key.startswith("state/")
        assert not key.startswith(f"state/{ws_id}/")
        assert not key.endswith(".tfstate")


class TestPromotion:
    async def test_the_last_of_many_checkpoints_becomes_one_version(self) -> None:
        ws, db = _ws(), _db(1)
        with _Harness() as h:
            for n in range(1, 4):
                await svc.hold_checkpoint(ws.id, "u-1", {"manifest": {"n": n}}, created_by="a@b.c")
            sv = await svc.promote_checkpoint(db, ws, "u-1")
        db.add.assert_called_once()
        assert sv is not None
        assert sv.serial == 2
        assert sv.created_by == "a@b.c"
        stored = h.store.objects[state_key(str(ws.id), str(sv.id))]
        assert json.loads(stored) == {"manifest": {"n": 3}}
        # Promoted, so no longer held.
        assert pulumi_checkpoint_key(str(ws.id), "u-1") not in h.store.objects

    async def test_an_update_that_wrote_nothing_leaves_no_version(self) -> None:
        db = _db(1)
        with _Harness() as h:
            assert await svc.promote_checkpoint(db, _ws(), "u-1") is None
        db.add.assert_not_called()
        h.record.assert_not_awaited()

    async def test_failing_to_remove_the_checkpoint_does_not_lose_the_version(self) -> None:
        ws, db = _ws(), _db(1)
        with _Harness() as h:
            await svc.hold_checkpoint(ws.id, "u-1", DEPLOYMENT, created_by=None)
            h.store.fail_delete = True
            sv = await svc.promote_checkpoint(db, ws, "u-1")
        assert sv is not None
        db.commit.assert_awaited_once()
