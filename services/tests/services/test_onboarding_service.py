"""Services-tier tests for the onboarding discovery service (#824 P2).

Covers the pure helpers and the D1 orchestration logic with the heavy subprocess
step mocked — the real ``tofu init`` + ``terrapod-query schema`` execution is
proven in the live P2.4 smoke, not here (mocked DB/Redis can't run tofu).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.db.models import OnboardingSession, Workspace
from terrapod.services import onboarding_service as svc


def _workspace(engine="tofu", version="1.12"):
    return SimpleNamespace(id=uuid.uuid4(), execution_backend=engine, terraform_version=version)


def _fake_db(session, workspace):
    async def _get(model, _pk):
        if model is OnboardingSession:
            return session
        if model is Workspace:
            return workspace
        return None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    return db


# --- pure helpers ----------------------------------------------------------
def test_surface_cache_key_shape():
    assert svc._surface_cache_key("tofu", "1.12", "aws") == "tp:onboard:surface:tofu:1.12:aws"


def test_provider_config_hcl_installs_provider_for_schema():
    hcl = svc._provider_config_hcl("aws")
    assert "required_providers" in hcl
    assert 'aws = { source = "aws" }' in hcl
    # Empty provider block — schema reads need no credentials or region.
    assert 'provider "aws" {}' in hcl


def test_local_platform_is_linux():
    os_, arch = svc._local_platform()
    assert os_ == "linux"
    assert arch in ("amd64", "arm64")


# --- orchestration ---------------------------------------------------------
@pytest.mark.asyncio
async def test_run_schema_discovery_cache_hit_skips_subprocess():
    """A warm surface cache short-circuits — no binary download, no tofu."""
    ws = _workspace()
    session = OnboardingSession(workspace_id=ws.id, provider="aws", status="pending")
    db = _fake_db(session, ws)
    cached = {"count": 3, "data_sources": [{"name": "aws_vpcs"}]}

    with (
        patch.object(svc, "get_cached_surface", AsyncMock(return_value=cached)),
        patch.object(svc, "_download_engine_binary", AsyncMock()) as dl,
    ):
        await svc.run_schema_discovery(db, session.id)

    assert session.status == "schema_ready"
    assert session.discovery_surface == cached
    dl.assert_not_called()  # cache hit → never touched the binary/subprocess


@pytest.mark.asyncio
async def test_run_schema_discovery_records_error_on_failure():
    """A failed discovery marks the session errored and never raises."""
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="pending")
    ws = _workspace()
    session.workspace_id = ws.id
    db = _fake_db(session, ws)

    with (
        patch.object(svc, "get_cached_surface", AsyncMock(return_value=None)),
        patch.object(svc, "_download_engine_binary", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        await svc.run_schema_discovery(db, session.id)

    assert session.status == "errored"
    assert "boom" in session.error


@pytest.mark.asyncio
async def test_run_schema_discovery_missing_workspace_errors_cleanly():
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="pending")
    db = _fake_db(session, None)  # workspace gone
    await svc.run_schema_discovery(db, session.id)
    assert session.status == "errored"
    assert "workspace" in session.error.lower()
