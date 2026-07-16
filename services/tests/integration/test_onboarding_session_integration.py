"""Integration tests: OnboardingSession model (#824 P2).

Exercises the real Postgres semantics the model relies on — column defaults
(server_default) and the ``workspace_id`` FK's ON DELETE CASCADE — which a
mocked test can't prove. The workspace is created via the API (so it gets all
its own defaults); the onboarding session is created at the model layer since
its endpoints land in a later phase.
"""

import uuid

import pytest
from sqlalchemy import select

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


async def _create_workspace(client, name: str) -> uuid.UUID:
    """Create a workspace via the API, return its internal UUID (resolved by name).

    The JSON:API ``data.id`` is not a bare UUID, so we look the row up by its
    unique name to get the real primary key the FK needs.
    """
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text

    from terrapod.db.models import Workspace
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.name == name))).scalar_one()
        return ws.id


async def test_onboarding_session_defaults(app, client):
    """A minimally-constructed session gets the intended server-side defaults."""
    from terrapod.db.models import OnboardingSession
    from terrapod.db.session import get_db_session

    set_auth(app, admin_user())
    ws_id = await _create_workspace(client, f"onb-defaults-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        sess = OnboardingSession(workspace_id=ws_id, provider="aws", created_by="admin")
        db.add(sess)
        await db.commit()
        sid = sess.id

    async with get_db_session() as db:
        got = await db.get(OnboardingSession, sid)
        assert got is not None
        assert got.status == "pending"
        assert got.selected_types == []
        assert got.query_results is None
        assert got.engine == ""
        assert got.engine_version == ""
        assert got.ai_assisted is False
        assert got.error == ""
        assert got.provider == "aws"
        assert got.discovery_run_id is None
        assert got.result_run_id is None
        assert got.created_at is not None and got.updated_at is not None


async def test_onboarding_session_cascades_on_workspace_delete(app, client):
    """Deleting the owning workspace deletes its onboarding sessions (FK CASCADE).

    Onboarding history is bounded to a workspace's lifetime — an orphaned
    session pointing at a gone workspace would be meaningless.
    """
    from terrapod.db.models import OnboardingSession, Workspace
    from terrapod.db.session import get_db_session

    set_auth(app, admin_user())
    ws_id = await _create_workspace(client, f"onb-cascade-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        sess = OnboardingSession(workspace_id=ws_id, provider="aws")
        db.add(sess)
        await db.commit()
        sid = sess.id

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        assert ws is not None
        await db.delete(ws)
        await db.commit()

    async with get_db_session() as db:
        assert await db.get(OnboardingSession, sid) is None
