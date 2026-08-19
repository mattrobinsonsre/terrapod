"""Service-tier tests for module impact analysis.

Module impact analysis fires on a module's PRs/publishes against the workspaces
that CONSUME it (the `module_workspace_link`). The consuming workspace's own VCS
status is irrelevant — yet `_fetch_workspace_config` used to return None for any
non-VCS workspace, silently excluding every non-VCS consumer (CLI-driven
workspaces, and later Service Catalog instances, #535) of a VCS-linked module.
The fix reuses the workspace's latest uploaded config-version when there is no
VCS to re-fetch.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import module_impact_service


def _non_vcs_workspace() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "catalog-instance"
    ws.vcs_connection_id = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    return ws


@pytest.mark.asyncio
async def test_fetch_config_reuses_latest_cv_for_non_vcs_workspace() -> None:
    ws = _non_vcs_workspace()
    db = AsyncMock()
    cv = MagicMock()
    cv.id = uuid.uuid4()

    with patch.object(
        module_impact_service.run_service,
        "get_latest_uploaded_cv",
        new=AsyncMock(return_value=cv),
    ) as m_latest:
        result = await module_impact_service._fetch_workspace_config(
            db, ws, MagicMock(), speculative=True
        )

    assert result == cv.id  # reused the catalog wrapper CV, not skipped
    m_latest.assert_awaited_once_with(db, ws.id)


@pytest.mark.asyncio
async def test_fetch_config_non_vcs_with_no_cv_returns_none() -> None:
    # A non-VCS workspace that has never had a CV uploaded has nothing to run.
    ws = _non_vcs_workspace()
    db = AsyncMock()

    with patch.object(
        module_impact_service.run_service,
        "get_latest_uploaded_cv",
        new=AsyncMock(return_value=None),
    ):
        result = await module_impact_service._fetch_workspace_config(db, ws, MagicMock())

    assert result is None


class TestModuleCommentUsesTheSnapshottedResult:
    """A module PR's comment must not render a plan result that has not landed.

    Two speculative runs on the same module PR, both `planned` with
    has_changes=False, rendered differently: one "No changes", the other
    "Plan finished" — which is what `_resolve_status` produces when has_changes
    is None. The enqueue happens inside the transaction that sets the status,
    so a consumer on another replica can re-read the row before the commit
    lands and see the PREVIOUS value. Which run loses the race is timing, which
    is why it looked arbitrary (#1378).

    The ordinary VCS path already snapshots the value into the payload for
    exactly this reason; this path did not.
    """

    def _payload_from_enqueue(self, run, target_status):
        """Capture what _enqueue_module_test_status puts on the queue."""
        import asyncio

        from terrapod.services import run_service

        captured = {}

        async def _fake_enqueue(name, payload, **kw):
            captured.update(payload)
            return True

        with patch("terrapod.services.scheduler.enqueue_trigger", new=_fake_enqueue):
            asyncio.run(run_service._enqueue_module_test_status(run, target_status))
        return captured

    def test_has_changes_is_snapshotted_onto_the_trigger(self):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.has_changes = False

        payload = self._payload_from_enqueue(run, "planned")

        assert "has_changes" in payload, (
            "without the snapshot the handler re-reads the row and can race the commit"
        )
        assert payload["has_changes"] is False

    def test_a_snapshotted_false_renders_no_changes_even_if_the_row_lags(self):
        """The bug, at the point where it showed: the row still says None
        (uncommitted) but the payload carries the real answer."""
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        stale_row_value = None
        snapshotted = False

        _, _, stale = _resolve_status("planned", True, stale_row_value)
        _, _, fixed = _resolve_status("planned", True, snapshotted)

        assert stale == "Plan finished"
        assert fixed == "No changes"

    def test_the_sentinel_keeps_a_genuine_none_distinguishable(self):
        """None means 'the plan did not record it' and must survive; only an
        ABSENT field falls back to the row. An older replica's trigger, raised
        mid-upgrade, carries no field at all."""
        from terrapod.services.module_impact_service import _UNSET

        assert _UNSET is not None
        assert {"has_changes": None}.get("has_changes", _UNSET) is None
        assert {}.get("has_changes", _UNSET) is _UNSET
