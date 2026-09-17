"""The `terrapod unlock` PR comment releases the whole lock, including its
reason and holder (#1705)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from terrapod.services import vcs_command_dispatcher


def _ws(**kw):
    base = {
        "name": "prod-network",
        "locked": True,
        "lock_id": "lock-ops@example.com",
        "lock_reason": "maintenance window",
        "locked_by": "ops@example.com",
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_unlock_clears_the_reason_and_holder():
    ws = _ws()
    db = AsyncMock()

    await vcs_command_dispatcher._route_unlock(db, [ws], "octocat", "12345")

    assert ws.locked is False
    assert ws.lock_id is None
    assert ws.lock_reason is None
    assert ws.locked_by is None
    db.commit.assert_awaited_once()


async def test_an_unlocked_workspace_is_left_untouched():
    ws = _ws(locked=False, lock_id=None, lock_reason=None, locked_by=None)
    db = AsyncMock()

    await vcs_command_dispatcher._route_unlock(db, [ws], "octocat", "12345")

    assert ws.locked is False
    assert ws.lock_reason is None
