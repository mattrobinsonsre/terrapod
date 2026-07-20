"""Tests for workspace_cost_service — workspace state-cost path (#871).

The cost engine itself is covered by test_cost_engine.py; here we exercise the
service's orchestration: the state:read gate, the no-state / missing-blob empty
paths, the pricesheet-unavailable failure, and that the priced result carries
the state-version meta. The engine call is mocked (don't test past the layer).
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.auth import capabilities as cap
from terrapod.services import workspace_cost_service as svc

WS = uuid.uuid4()
SV = uuid.uuid4()

_ENGINE_RESULT = {
    "currency": "USD",
    "total": {"min": 292.0, "max": 292.0},
    "previous": {"min": 292.0, "max": 292.0},
    "diff": {"min": 0.0, "max": 0.0},
    "resources": [
        {
            "address": "aws_instance.web",
            "type": "aws_instance",
            "name": "web",
            "change": "noop",
            "monthly": {"min": 73.0, "max": 73.0},
        }
    ],
    "unpriced": [{"address": "random_pet.name", "type": "random_pet", "change": "noop"}],
}


def _ws():
    return SimpleNamespace(id=WS, name="prod", owner_email="o@x.io", labels={})


def _sv(state_size=1234):
    return SimpleNamespace(
        id=SV,
        workspace_id=WS,
        serial=7,
        state_size=state_size,
        created_at=datetime(2026, 7, 20, 9, 0, 0, tzinfo=UTC),
    )


def _db_returning(sv):
    """AsyncMock db whose single execute(...).scalar_one_or_none() yields sv."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=sv)))
    return db


def _user():
    return SimpleNamespace(email="u@x.io", roles=["everyone"])


def _patches(caps, sv, *, state_bytes=b"{}", engine=_ENGINE_RESULT):
    """Stack the service's collaborators; caller supplies caps + state version."""
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=state_bytes)
    return [
        patch("terrapod.api.routers.tfe_v2._get_workspace_by_id", AsyncMock(return_value=_ws())),
        patch.object(svc, "resolve_workspace_capabilities_for", AsyncMock(return_value=caps)),
        patch.object(svc, "get_storage", return_value=storage),
        patch("terrapod.crypto.state.decrypt_state_bytes", AsyncMock(side_effect=lambda b: b)),
        patch.object(
            svc.cost_pricesheet_service, "ensure_pricesheet", AsyncMock(return_value=True)
        ),
        patch.object(
            svc.cost_pricesheet_service,
            "download_cached_to_file",
            AsyncMock(return_value="/tmp/prices.csv"),
        ),
        patch.object(svc.cost_pricesheet_service, "_safe_unlink", MagicMock()),
        patch.object(svc, "_run_engine", MagicMock(return_value=dict(engine))),
    ]


class _Ctx:
    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


async def test_denied_without_state_read():
    with _Ctx(_patches(frozenset(), _sv())):
        with pytest.raises(HTTPException) as ei:
            await svc.estimate_workspace_cost(_db_returning(_sv()), _user(), "ws-" + str(WS))
    assert ei.value.status_code == 403


async def test_no_state_returns_zeroed_empty():
    with _Ctx(_patches(frozenset({cap.STATE_READ}), None)):
        attrs = await svc.estimate_workspace_cost(_db_returning(None), _user(), str(WS))
    assert attrs["state-version"] is None
    assert attrs["total"] == {"min": 0.0, "max": 0.0}
    assert attrs["resources"] == [] and attrs["unpriced"] == []


async def test_unlanded_state_version_is_empty():
    sv = _sv(state_size=0)
    with _Ctx(_patches(frozenset({cap.STATE_READ}), sv)):
        attrs = await svc.estimate_workspace_cost(_db_returning(sv), _user(), str(WS))
    # A row that exists before its /content PUT landed → empty, not an error.
    assert attrs["state-version"] is None
    assert attrs["total"]["max"] == 0.0


async def test_missing_blob_is_empty_with_sv_meta():
    sv = _sv()
    patches = _patches(frozenset({cap.STATE_READ}), sv)
    # Override storage.get to raise (metadata row, no backing object).
    storage = AsyncMock()
    storage.get = AsyncMock(side_effect=RuntimeError("gone"))
    patches[2] = patch.object(svc, "get_storage", return_value=storage)
    with _Ctx(patches):
        attrs = await svc.estimate_workspace_cost(_db_returning(sv), _user(), str(WS))
    # Blob gone → zeroed, but the state-version meta is still surfaced.
    assert attrs["state-version"]["id"] == f"sv-{SV}"
    assert attrs["total"]["max"] == 0.0


async def test_pricesheet_unavailable_raises():
    sv = _sv()
    patches = _patches(frozenset({cap.STATE_READ}), sv)
    patches[4] = patch.object(
        svc.cost_pricesheet_service, "ensure_pricesheet", AsyncMock(return_value=False)
    )
    with _Ctx(patches):
        with pytest.raises(svc.PricesheetUnavailable):
            await svc.estimate_workspace_cost(_db_returning(sv), _user(), str(WS))


async def test_happy_path_prices_and_attaches_sv_meta():
    sv = _sv()
    with _Ctx(_patches(frozenset({cap.STATE_READ}), sv)):
        attrs = await svc.estimate_workspace_cost(_db_returning(sv), _user(), str(WS))
    assert attrs["total"]["max"] == 292.0
    assert attrs["resources"][0]["change"] == "noop"
    assert attrs["state-version"] == {
        "id": f"sv-{SV}",
        "serial": 7,
        "created-at": "2026-07-20T09:00:00Z",
    }


async def test_pricesheet_tempfile_is_always_unlinked():
    """The PVC pricesheet tempfile is cleaned up even when the engine raises."""
    sv = _sv()
    patches = _patches(frozenset({cap.STATE_READ}), sv)
    unlink = MagicMock()
    patches[6] = patch.object(svc.cost_pricesheet_service, "_safe_unlink", unlink)
    patches[7] = patch.object(svc, "_run_engine", MagicMock(side_effect=ValueError("bad state")))
    with _Ctx(patches):
        with pytest.raises(ValueError):
            await svc.estimate_workspace_cost(_db_returning(sv), _user(), str(WS))
    unlink.assert_called_once_with("/tmp/prices.csv")
