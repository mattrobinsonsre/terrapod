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
    # Includes the provider-version constraint segment (empty = latest) so a v5
    # and a v6 surface never collide.
    assert svc._surface_cache_key("tofu", "1.12", "aws", "") == "tp:onboard:surface:tofu:1.12:aws:"


def test_surface_cache_key_separates_provider_versions():
    latest = svc._surface_cache_key("tofu", "1.12", "aws", "")
    pinned = svc._surface_cache_key("tofu", "1.12", "aws", "< 6.0")
    assert latest != pinned
    assert pinned.endswith(":< 6.0")


def test_provider_config_hcl_installs_provider_for_schema():
    hcl = svc._provider_config_hcl("aws")
    assert "required_providers" in hcl
    assert 'aws = { source = "aws" }' in hcl
    # Empty provider block — schema reads need no credentials or region.
    assert 'provider "aws" {}' in hcl


def test_provider_config_hcl_pins_version_constraint():
    hcl = svc._provider_config_hcl("aws", "< 6.0")
    assert 'aws = { source = "aws", version = "< 6.0" }' in hcl
    # No constraint → no version key (resolves latest).
    assert "version =" not in svc._provider_config_hcl("aws")


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("", True),
        ("< 6.0", True),
        ("~> 5.0", True),
        (">= 5.1, < 6.0", True),
        ("6", True),
        ('6" }\nmalicious', False),  # can't break out of the HCL string literal
        ("garbage", False),
        ("aws", False),
    ],
)
def test_is_valid_version_constraint(value, ok):
    assert svc.is_valid_version_constraint(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("aws", True),
        ("google", True),
        ("azurerm", True),
        ("aws-cc", True),  # hyphen allowed inside the name
        ("", False),  # empty rejected
        ("AWS", False),  # uppercase rejected (interpolated into `provider "<name>"`)
        ("1aws", False),  # must start with a letter
        ('aws" }\nmalicious', False),  # can't break out of the HCL string literal
        ("aws provider", False),  # no whitespace
        ("a" * 65, False),  # over length cap
    ],
)
def test_is_valid_provider(value, ok):
    assert svc.is_valid_provider(value) is ok


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
    # Surface is Redis-only — pinned by the small engine/version scalars, never
    # written to the session row.
    assert session.engine == "tofu"
    assert session.engine_version == "1.12"
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


# --- D2/D3 dispatch: start_discovery ---------------------------------------
def _agent_ws():
    return SimpleNamespace(
        id=uuid.uuid4(),
        execution_backend="tofu",
        terraform_version="1.12",
        execution_mode="agent",
        agent_pool_links=[SimpleNamespace(agent_pool_id=uuid.uuid4(), ordinal=0, agent_pool=None)],
        auto_apply=False,
        terragrunt_enabled=False,
        terragrunt_version="",
        resource_cpu="1",
        parallelism=10,
        resource_memory="2Gi",
        name="ws",
    )


@pytest.mark.asyncio
async def test_start_discovery_requires_schema_ready():
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="pending")
    db = AsyncMock()
    with pytest.raises(svc.OnboardingError):
        await svc.start_discovery(db, session, ["aws_vpcs"])


@pytest.mark.asyncio
async def test_start_discovery_requires_agent_pool():
    ws = SimpleNamespace(id=uuid.uuid4(), execution_mode="local", agent_pool_links=[])
    session = OnboardingSession(workspace_id=ws.id, provider="aws", status="schema_ready")
    db = _fake_db(session, ws)
    with pytest.raises(svc.OnboardingError):
        await svc.start_discovery(db, session, ["aws_vpcs"])


@pytest.mark.asyncio
async def test_start_discovery_rejects_selection_not_in_surface():
    ws = _agent_ws()
    session = OnboardingSession(
        workspace_id=ws.id,
        provider="aws",
        status="schema_ready",
        engine="tofu",
        engine_version="1.12",
    )
    db = _fake_db(session, ws)
    surface = {"data_sources": [{"name": "aws_vpcs"}]}
    with patch.object(svc, "get_session_surface", AsyncMock(return_value=surface)):
        with pytest.raises(svc.OnboardingError):
            await svc.start_discovery(db, session, ["not_a_real_type"])


@pytest.mark.asyncio
async def test_start_discovery_happy_path_creates_run_and_transitions():
    ws = _agent_ws()
    session = OnboardingSession(
        workspace_id=ws.id,
        provider="aws",
        status="schema_ready",
        engine="tofu",
        engine_version="1.12",
        created_by="u@x",
    )
    db = _fake_db(session, ws)
    surface = {"data_sources": [{"name": "aws_vpcs"}, {"name": "aws_subnets"}]}
    fake_run = SimpleNamespace(id=uuid.uuid4())
    with (
        patch.object(svc, "get_session_surface", AsyncMock(return_value=surface)),
        patch("terrapod.services.run_service.create_run", AsyncMock(return_value=fake_run)) as cr,
        patch("terrapod.services.run_service.queue_run", AsyncMock()) as qr,
    ):
        # dupes + an unknown type are dropped; order preserved.
        await svc.start_discovery(db, session, ["aws_vpcs", "bogus", "aws_vpcs"])

    assert session.status == "querying"
    assert session.selected_types == ["aws_vpcs"]
    assert session.discovery_run_id == fake_run.id
    cr.assert_awaited_once()
    assert cr.await_args.kwargs["source"] == "onboarding-discovery"
    assert cr.await_args.kwargs["plan_only"] is True
    qr.assert_awaited_once()


# --- reconciler hook: complete_discovery -----------------------------------
def _db_returning(session):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: session))
    return db


@pytest.mark.asyncio
async def test_complete_discovery_success_with_config_is_config_ready():
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    session.generated_config = 'resource "aws_vpc" "x" {}'
    db = _db_returning(session)
    await svc.complete_discovery(db, uuid.uuid4(), success=True)
    assert session.status == "config_ready"


@pytest.mark.asyncio
async def test_complete_discovery_success_without_config_is_config_ready_nothing_found():
    # A successful run that produced no config is the legitimate "no unmanaged
    # resources of the selected types were found" outcome — a clean terminal
    # state, NOT an error. The runner only exits 0 once its uploads land (or
    # there was nothing to upload), so success is trustworthy here.
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    session.generated_config = None
    db = _db_returning(session)
    await svc.complete_discovery(db, uuid.uuid4(), success=True)
    assert session.status == "config_ready"
    assert session.error == ""


@pytest.mark.asyncio
async def test_complete_discovery_failure_errors_with_message():
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    db = _db_returning(session)
    await svc.complete_discovery(db, uuid.uuid4(), success=False, error="boom")
    assert session.status == "errored"
    assert "boom" in session.error


@pytest.mark.asyncio
async def test_complete_discovery_ignores_already_resolved_session():
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="config_ready")
    db = _db_returning(session)
    await svc.complete_discovery(db, uuid.uuid4(), success=False, error="late")
    # An already-terminal session is not clobbered by a late reconciler pass.
    assert session.status == "config_ready"


# --- AI polish enqueue on config_ready (#824 Phase A) ----------------------
@pytest.mark.asyncio
async def test_complete_discovery_enqueues_polish_when_ai_enabled(monkeypatch):
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    session.generated_config = 'resource "aws_vpc" "x" {}'
    db = _db_returning(session)
    monkeypatch.setattr(svc.settings.ai_onboarding, "enabled", True)
    enqueue = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enqueue):
        await svc.complete_discovery(db, uuid.uuid4(), success=True)
    assert enqueue.await_count == 1
    assert enqueue.await_args.args[0] == "onboarding_polish"
    assert enqueue.await_args.args[1] == {"session_id": str(session.id)}


@pytest.mark.asyncio
async def test_complete_discovery_no_polish_when_ai_disabled(monkeypatch):
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    session.generated_config = 'resource "aws_vpc" "x" {}'
    db = _db_returning(session)
    monkeypatch.setattr(svc.settings.ai_onboarding, "enabled", False)
    enqueue = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enqueue):
        await svc.complete_discovery(db, uuid.uuid4(), success=True)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_discovery_no_polish_when_nothing_found(monkeypatch):
    # AI on, but discovery found no resources → no config to polish → no enqueue.
    session = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="querying")
    session.generated_config = None
    db = _db_returning(session)
    monkeypatch.setattr(svc.settings.ai_onboarding, "enabled", True)
    enqueue = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enqueue):
        await svc.complete_discovery(db, uuid.uuid4(), success=True)
    enqueue.assert_not_awaited()
