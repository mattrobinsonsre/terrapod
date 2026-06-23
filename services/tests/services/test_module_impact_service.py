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
