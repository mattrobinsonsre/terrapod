"""The break-glass state index: `state/index.yaml` in object storage (#1581).

The index maps each workspace name to the object key of its latest state
version, so an operator who has lost PostgreSQL can still find every
workspace's state by downloading one file. It is only worth having if it is
current: `blob_classes._resolve_state_index` says a stale index is worse than
none, because it points at the wrong objects while looking authoritative.

**Every path that writes a state version records it here.** Until #1581 only
the CLI's local-mode upload and a workspace rename did, so the index named the
last CLI upload — or nothing — for every agent-mode workspace, which is to say
for every workspace run the normal way. A guard test now fails the build if a
module constructs a `StateVersion` without calling `record_latest_state`.

Three properties the old helper in `tfe_v2.py` lacked:

- **A read error never wipes the index.** It treated any failure to read the
  index as "no index yet" and wrote back a one-entry file, so a transient
  storage error erased every other workspace. Only a genuine not-found starts a
  fresh index now; any other failure leaves the object as it is.
- **Writers are serialised.** The index is one object updated read-modify-write,
  and every agent-run apply now writes it, from any API replica. A short Redis
  lock stops two writers each dropping the other's entry. If the lock cannot be
  had, the update proceeds anyway: a rare race costs one entry until that
  workspace's next write, where skipping would cost it for certain.
- **Parsing is off the event loop.** The index grows with the workspace count,
  and a PyYAML parse of a large one would stall the replica (CLAUDE.md #13).

Best-effort throughout, as before: an index failure is logged and never fails
the state write that triggered it.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from terrapod.logging_config import get_logger
from terrapod.storage import get_storage
from terrapod.storage.keys import state_index_key, state_key
from terrapod.storage.protocol import ObjectNotFoundError

logger = get_logger(__name__)

#: Held while one replica rewrites the index.
LOCK_KEY = "tp:state_index:lock"
#: Long enough for a read, a parse and a write of a large index, and short
#: enough that a replica dying mid-write does not stall the rest for long.
LOCK_TTL_SECONDS = 30
#: How long a writer waits for the lock before updating without it.
LOCK_WAIT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


def _load(raw: bytes) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(raw) if raw else None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"the state index is a {type(data).__name__}, not a mapping")
    return data


def _dump(index: dict[str, Any]) -> bytes:
    import yaml

    return yaml.dump(index, default_flow_style=False).encode()


async def _acquire_lock() -> tuple[Any, str | None]:
    """Take the index lock. `(None, None)` means "carry on without it"."""
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
    except Exception:  # noqa: BLE001 — no Redis is not a reason to skip the update
        return None, None

    token = secrets.token_hex(8)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            if await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS):
                return redis, token
        except Exception:  # noqa: BLE001
            return None, None
        if time.monotonic() >= deadline:
            logger.warning("State index lock not acquired in time; updating without it")
            return None, None
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _release_lock(redis: Any, token: str | None) -> None:
    if redis is None or token is None:
        return
    try:
        held = await redis.get(LOCK_KEY)
        if held in (token, token.encode()):
            await redis.delete(LOCK_KEY)
    except Exception:  # noqa: BLE001 — the TTL releases it regardless
        pass


async def _mutate(change: Callable[[dict[str, Any]], bool], *, action: str, workspace: str) -> None:
    """Read the index, apply `change`, and write it back if `change` says so."""
    redis, token = await _acquire_lock()
    try:
        storage = get_storage()
        key = state_index_key()
        try:
            raw = await storage.get(key)
        except ObjectNotFoundError:
            raw = b""
        index = await asyncio.to_thread(_load, raw)
        if not change(index):
            return
        body = await asyncio.to_thread(_dump, index)
        await storage.put(key, body, content_type="application/x-yaml")
    except Exception:  # noqa: BLE001 — index updates must never break state writes
        logger.warning("State index not updated", action=action, workspace=workspace, exc_info=True)
    finally:
        await _release_lock(redis, token)


async def record_latest_state(
    *,
    workspace_name: str,
    workspace_id: uuid.UUID | str,
    state_version_id: uuid.UUID | str,
    serial: int,
) -> None:
    """Record a workspace's latest state version in the index.

    Called after the state version is committed. An entry already naming a
    newer serial for the same workspace is left alone, so a slow writer cannot
    roll the index back; an entry for a different workspace id under the same
    name (a restore, or a name reused after a delete) is replaced.
    """
    ws_id = str(workspace_id)
    entry = {
        "workspace_id": ws_id,
        "state_key": state_key(ws_id, str(state_version_id)),
        "serial": serial,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def change(index: dict[str, Any]) -> bool:
        current = index.get(workspace_name)
        if (
            isinstance(current, dict)
            and current.get("workspace_id") == ws_id
            and isinstance(current.get("serial"), int)
            and current["serial"] > serial
        ):
            return False
        index[workspace_name] = entry
        return True

    await _mutate(change, action="record", workspace=workspace_name)


async def remove_workspace(workspace_name: str) -> None:
    """Drop a workspace from the index — a delete, or the old name on a rename."""

    def change(index: dict[str, Any]) -> bool:
        return index.pop(workspace_name, None) is not None

    await _mutate(change, action="remove", workspace=workspace_name)
