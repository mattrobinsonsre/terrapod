"""Tests for execution_hook_service (#619): validation + resolution."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from terrapod.db.models import EXECUTION_HOOK_POINTS
from terrapod.services import execution_hook_service as svc


class TestValidateHookPoint:
    def test_all_valid_points_pass(self) -> None:
        for point in EXECUTION_HOOK_POINTS:
            svc.validate_hook_point(point)  # no raise

    def test_invalid_point_422(self) -> None:
        with pytest.raises(HTTPException) as ei:
            svc.validate_hook_point("post_destroy")
        assert ei.value.status_code == 422

    def test_empty_point_422(self) -> None:
        with pytest.raises(HTTPException) as ei:
            svc.validate_hook_point("")
        assert ei.value.status_code == 422


def _hook(hook_point: str, name: str, script: str) -> MagicMock:
    h = MagicMock()
    h.hook_point = hook_point
    h.name = name
    h.script = script
    return h


class TestResolveHooksForWorkspace:
    async def test_maps_shape_in_delivered_order(self) -> None:
        # The resolver returns whatever the (already-ordered) query yields,
        # mapped to the runner-side dict shape.
        rows = [
            _hook("pre_init", "a", "echo a"),
            _hook("post_apply", "b", "echo b"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        out = await svc.resolve_hooks_for_workspace(db, uuid.uuid4())

        assert out == [
            {"hook_point": "pre_init", "name": "a", "script": "echo a"},
            {"hook_point": "post_apply", "name": "b", "script": "echo b"},
        ]

    async def test_empty_when_no_hooks(self) -> None:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        out = await svc.resolve_hooks_for_workspace(db, uuid.uuid4())
        assert out == []
