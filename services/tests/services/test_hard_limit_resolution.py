"""The ceiling bounds every reader of the inventory, not just the run (#2451).

`bounded_limit` sits at the one boundary every reader passes through, because
the ceiling has to hold for the run's `-i` document, the `?limit=` preview, the
stored artifact, the OPA policy input and whatever the UI renders. A per-caller
rule is how one of those comes to show an operator hosts the workspace can never
touch -- which is worse than no ceiling, because it is a bound that reads as
enforced and is not.

Three properties, each a contrast rather than a happy path:

* **Both entry points bound.** `resolve_for_run` and `resolve` are separate
  functions with separate gates, and a fixture driving only one of them passes
  whichever way the other is written.
* **`resolve` bounds BEFORE the cache key.** The key carries the limit, so
  bounding afterwards would let two different ceilings share one entry -- and
  the symptom is a cached document from another workspace's ceiling, which no
  amount of reading the resolution finds.
* **A workspace with no ceiling is untouched.** This is what makes the feature
  additive; a test suite that only ever set one would pass with the bound
  applied unconditionally.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import inventory_resolve as ir


def _ws(hard_limit: str = "", **kw):
    ws = MagicMock()
    ws.hard_limit = hard_limit
    ws.configure_enabled = kw.pop("configure_enabled", True)
    ws.vcs_connection_id = kw.pop("vcs_connection_id", None)
    ws.vcs_repo_url = kw.pop("vcs_repo_url", None)
    for k, v in kw.items():
        setattr(ws, k, v)
    return ws


class TestBoundedLimit:
    @pytest.mark.parametrize(
        ("hard", "limit", "expected"),
        [
            # No ceiling: the caller's limit reaches ansible untouched, which is
            # what keeps every existing deployment behaving exactly as before.
            ("", "web", "web"),
            ("", None, None),
            ("", "", ""),
            # A ceiling and no caller limit: the ceiling IS the limit.
            ("canary", None, "canary"),
            ("canary", "", "canary"),
            # Both: intersected, with the caller's pattern kept verbatim.
            ("canary", "web", "web:&canary"),
            ("canary", "web:prod", "web:prod:&canary"),
            ("!prod", "web", "web:!prod"),
        ],
    )
    def test_bounding(self, hard, limit, expected):
        assert ir.bounded_limit(_ws(hard), limit) == expected

    def test_no_workspace_means_no_ceiling(self):
        """A workspace that is not an inventory workspace has none to apply."""
        assert ir.bounded_limit(None, "web") == "web"
        assert ir.bounded_limit(None, None) is None

    def test_a_workspace_without_the_attribute_is_tolerated(self):
        """Replication and restore both hand this older row shapes."""
        bare = object()
        assert ir.bounded_limit(bare, "web") == "web"


class TestBothEntryPointsBound:
    async def test_resolve_for_run_bounds_the_runs_limit(self):
        run = MagicMock()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None
        inv = MagicMock()
        inv.get_inventory_workspace = AsyncMock(return_value=_ws("canary"))

        with (
            patch.dict("sys.modules"),
            patch.object(ir, "_resolve_uncached", AsyncMock(return_value={})) as uncached,
            patch(
                "terrapod.services.inventory_service.get_inventory_workspace",
                inv.get_inventory_workspace,
            ),
        ):
            await ir.resolve_for_run(MagicMock(), run, limit="web")

        assert uncached.await_args.kwargs["limit"] == "web:&canary"

    async def test_resolve_for_run_leaves_an_unbounded_workspace_alone(self):
        run = MagicMock()
        run.workspace_id = uuid.uuid4()
        run.configuration_version_id = None

        with (
            patch.object(ir, "_resolve_uncached", AsyncMock(return_value={})) as uncached,
            patch(
                "terrapod.services.inventory_service.get_inventory_workspace",
                AsyncMock(return_value=_ws("")),
            ),
        ):
            await ir.resolve_for_run(MagicMock(), run, limit="web")

        assert uncached.await_args.kwargs["limit"] == "web"

    async def test_resolve_bounds_the_preview_limit(self):
        ws_id = uuid.uuid4()
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock()

        with (
            patch.object(ir, "_resolve_uncached", AsyncMock(return_value={})) as uncached,
            patch.object(ir, "platform_rev", AsyncMock(return_value="rev1")),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(
                "terrapod.services.inventory_service.get_inventory_workspace",
                AsyncMock(return_value=_ws("canary")),
            ),
        ):
            await ir.resolve(MagicMock(), ws_id, limit="web")

        assert uncached.await_args.kwargs["limit"] == "web:&canary"


class TestResolveBoundsBeforeTheCacheKey:
    async def _key_for(self, hard: str, limit: str | None) -> str:
        ws_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.setex = AsyncMock()

        with (
            patch.object(ir, "_resolve_uncached", AsyncMock(return_value={})),
            patch.object(ir, "platform_rev", AsyncMock(return_value="rev1")),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(
                "terrapod.services.inventory_service.get_inventory_workspace",
                AsyncMock(return_value=_ws(hard)),
            ),
        ):
            await ir.resolve(MagicMock(), ws_id, limit=limit)
        return redis.get.await_args.args[0]

    async def test_two_ceilings_over_one_limit_do_not_share_an_entry(self):
        """The defect a post-key bound would cause, asserted directly.

        Both reads ask for `web`. If the ceiling were applied after the key was
        computed the two would hash identically, and the second workspace would
        be served the first one's document -- hosts its own ceiling forbids,
        from a cache hit, with nothing in the resolution to explain it.
        """
        a = await self._key_for("canary", "web")
        b = await self._key_for("prod", "web")
        assert a != b

    async def test_no_ceiling_keys_the_same_as_before(self):
        """So adding the column invalidates nothing already cached."""
        assert await self._key_for("", "web") == ir.cache_key(
            uuid.UUID("11111111-1111-1111-1111-111111111111"), "rev1", "", "web"
        )

    async def test_the_ceiling_alone_is_in_the_key(self):
        """A preview with no limit of its own still keys on the ceiling."""
        assert await self._key_for("canary", None) != await self._key_for("", None)
