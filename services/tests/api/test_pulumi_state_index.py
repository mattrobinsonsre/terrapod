"""The Pulumi service's own state write records the break-glass index (#1581).

`_write_deployment` is how local-mode Pulumi state lands — every checkpoint and
every `stack import`. It used to leave `state/index.yaml` untouched.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


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


async def _write(latest_serial: int | None, *, put_error: Exception | None = None) -> AsyncMock:
    from terrapod.api.routers.pulumi_service import _write_deployment

    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::dev"
    storage = MagicMock()
    storage.put = AsyncMock(side_effect=put_error)
    record = AsyncMock()
    with (
        patch("terrapod.storage.get_storage", return_value=storage),
        patch("terrapod.crypto.state.encrypt_state_bytes", AsyncMock(side_effect=lambda b: b)),
        patch("terrapod.services.run_service.discard_stale_plans_for_state_change", AsyncMock()),
        patch("terrapod.services.state_index_service.record_latest_state", record),
    ):
        if put_error is None:
            await _write_deployment(ws, _db(latest_serial), {"resources": []})
        else:
            # The write fails before the index is touched, and says so.
            with pytest.raises(type(put_error)):
                await _write_deployment(ws, _db(latest_serial), {"resources": []})
    return record


async def test_a_checkpoint_is_recorded_as_the_latest_state() -> None:
    record = await _write(latest_serial=1)
    record.assert_awaited_once()
    assert record.await_args.kwargs["serial"] == 2
    assert record.await_args.kwargs["workspace_name"] == "proj::dev"


async def test_the_first_write_is_recorded_too() -> None:
    record = await _write(latest_serial=None)
    assert record.await_args.kwargs["serial"] == 1


async def test_nothing_is_recorded_when_the_state_never_landed() -> None:
    """The index must never name an object that was not written."""
    record = await _write(latest_serial=1, put_error=RuntimeError("storage down"))
    record.assert_not_awaited()
