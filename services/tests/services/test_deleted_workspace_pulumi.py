"""Deleted Pulumi stacks restore with their state, and `stack rm` is the native delete (#1564).

The restore round trip against real Postgres and storage is in
`tests/integration/test_deleted_workspace_restore.py`; these pin the pieces it
is built from.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import deleted_workspace_service as dws

T0 = datetime(2026, 9, 14, tzinfo=UTC)


class TestPulumiFacts:
    def test_the_caller_numbers_the_version(self) -> None:
        body = json.dumps({"manifest": {}, "resources": []}).encode()
        assert dws._pulumi_facts(body, 3) == {
            "serial": 3,
            "lineage": "",
            "md5": hashlib.md5(body).hexdigest(),  # noqa: S324
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }

    def test_an_empty_stack_is_restorable(self) -> None:
        """A stack with no resources exports `deployment: null`."""
        assert dws._pulumi_facts(b"null", 1)["serial"] == 1

    @pytest.mark.parametrize("body", [b"[1, 2]", b"not json"])
    def test_anything_else_is_refused(self, body: bytes) -> None:
        with pytest.raises(ValueError):
            dws._pulumi_facts(body, 1)


def _meta(key: str, minutes: int) -> SimpleNamespace:
    return SimpleNamespace(key=key, last_modified=T0 + timedelta(minutes=minutes))


def _key(ws: str, ident: uuid.UUID) -> str:
    return f"state/{ws}/{ident}.tfstate"


class TestPulumiVersionsAreOrderedByAge:
    """A deployment has no serial, so order is all a restore can number by."""

    def test_time_ordered_ids_keep_their_order(self) -> None:
        ws = str(uuid.uuid4())
        keys = sorted(_key(ws, uuid.uuid7()) for _ in range(3))
        # Write times deliberately disagree: replication stamps fresh ones, so
        # when the ids carry the order the ids win.
        objects = [_meta(k, -n) for n, k in enumerate(keys)]
        assert dws._oldest_first(objects, keys) == keys

    def test_random_ids_are_ordered_by_write_time(self) -> None:
        """What the service surface wrote before #1564."""
        ws = str(uuid.uuid4())
        older, newer = _key(ws, uuid.uuid4()), _key(ws, uuid.uuid4())
        keys = sorted([older, newer])
        objects = [_meta(older, 1), _meta(newer, 2)]
        assert dws._oldest_first(objects, keys) == [older, newer]

    def test_a_mixed_history_is_ordered_by_write_time(self) -> None:
        ws = str(uuid.uuid4())
        legacy, current = _key(ws, uuid.uuid4()), _key(ws, uuid.uuid7())
        objects = [_meta(current, 5), _meta(legacy, 1)]
        assert dws._oldest_first(objects, sorted([legacy, current])) == [legacy, current]


class TestTheOneDeletePath:
    """`pulumi stack rm` and the native delete share it."""

    async def test_the_marker_is_built_first_and_written_after_the_commit(self) -> None:
        order: list[str] = []
        db = AsyncMock()
        db.delete.side_effect = lambda _ws: order.append("delete")
        db.commit.side_effect = lambda: order.append("commit")
        ws = SimpleNamespace(id=uuid.uuid4(), name="proj::dev")
        build = AsyncMock(side_effect=lambda *a, **k: order.append("build") or {"m": 1})
        write = AsyncMock(side_effect=lambda *a: order.append("write"))
        unindex = AsyncMock(side_effect=lambda name: order.append(f"unindex:{name}"))
        with (
            patch.object(dws, "build_marker", build),
            patch.object(dws, "write_marker_best_effort", write),
            patch("terrapod.services.state_index_service.remove_workspace", unindex),
        ):
            await dws.delete_workspace(db, ws, deleted_by="a@b.c")
        assert order == ["build", "delete", "commit", "write", "unindex:proj::dev"]
        build.assert_awaited_once_with(db, ws, deleted_by="a@b.c")
        write.assert_awaited_once_with(str(ws.id), {"m": 1})

    def test_the_native_delete_uses_it(self) -> None:
        import pathlib

        routers = pathlib.Path(dws.__file__).resolve().parent.parent / "api" / "routers"
        for name in ("tfe_v2.py", "pulumi_service.py"):
            src = (routers / name).read_text()
            assert "delete_workspace(db, ws, deleted_by=" in src, name
            assert "build_marker(" not in src, name
