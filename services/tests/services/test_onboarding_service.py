"""Services-tier tests for the onboarding discovery service (#824 P2).

Covers the pure helpers and the D1 orchestration logic with the heavy subprocess
step mocked — the real ``tofu init`` + ``terrapod-query schema`` execution is
proven in the live P2.4 smoke, not here (mocked DB/Redis can't run tofu).
"""

import io
import os
import tempfile
import uuid
import zipfile
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


# --- subprocess environment: the allowlist ---------------------------------
# The API process environment holds the key-encryption key, the token signing key
# and the database DSN, and `terrapod-query schema` makes the engine launch the
# provider plugin as a child. These tests pin BOTH directions: the secrets are
# gone, and everything a proxied / custom-CA deployment needs survives. The
# second half is the one that matters operationally — an over-narrow allowlist
# breaks air-gapped and egress-proxied installs, and nothing else would catch it.
_API_SECRETS = {
    "TERRAPOD_DATABASE_URL": "postgresql+asyncpg://u:p@db/terrapod",
    "TERRAPOD_ENCRYPTION__STATIC_KEK": "a-key-encryption-key",
    "TERRAPOD_TOKEN_SIGNING_KEY": "a-token-signing-key",
    "TERRAPOD_REDIS_URL": "redis://redis:6379/0",
    "TP_AUTH_TOKEN": "runtok:abc",
    "AWS_SECRET_ACCESS_KEY": "cloud-credential",
    "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/token",
}

# Every one of these is read from the environment and nowhere else, so dropping
# any of them silently breaks a real deployment (see `_ENGINE_ENV_KEYS`).
_MUST_SURVIVE = {
    "PATH": "/usr/local/bin:/usr/bin",
    "HOME": "/home/terrapod",
    "TMPDIR": "/var/lib/terrapod/tmp",
    "TF_CLI_CONFIG_FILE": "/etc/terrapod/terraform.rc",
    "TF_PLUGIN_CACHE_DIR": "/var/lib/terrapod/plugins",
    "TF_REGISTRY_CLIENT_TIMEOUT": "30",
    "TF_PROVIDER_DOWNLOAD_RETRY": "3",
    "HTTP_PROXY": "http://proxy.internal:3128",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "NO_PROXY": "localhost,.svc",
    "http_proxy": "http://proxy.internal:3128",
    "https_proxy": "http://proxy.internal:3128",
    "no_proxy": "localhost,.svc",
    "SSL_CERT_FILE": "/etc/terrapod-ca/ca-bundle.crt",
    "SSL_CERT_DIR": "/etc/ssl/certs",
    "CURL_CA_BUNDLE": "/etc/terrapod-ca/ca-bundle.crt",
    "REQUESTS_CA_BUNDLE": "/etc/terrapod-ca/ca-bundle.crt",
    "GIT_SSL_CAINFO": "/etc/terrapod-ca/ca-bundle.crt",
    "TF_TOKEN_registry_example_com": "a-private-registry-token",
    "TF_CLI_ARGS_init": "-plugin-dir=/var/lib/terrapod/plugins",
}


def _capture_engine_envs(tmp_path, monkeypatch, overrides):
    """Run the blocking discovery with both subprocesses faked; return their envs."""
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    captured: list[dict[str, str]] = []

    def _fake_run(_argv, **kwargs):
        captured.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout='{"count": 0, "data_sources": []}', stderr="")

    with patch.object(svc.subprocess, "run", _fake_run):
        svc._discover_surface_blocking("/usr/local/bin/tofu", "aws", str(tmp_path))
    # init + schema — the allowlist must cover both, not just the first.
    assert len(captured) == 2
    return captured


def test_engine_env_withholds_the_api_process_secrets(tmp_path, monkeypatch):
    for env in _capture_engine_envs(tmp_path, monkeypatch, _API_SECRETS):
        for key in _API_SECRETS:
            assert key not in env, f"{key} reached the engine subprocess"


def test_engine_env_keeps_the_proxy_and_tls_passthrough(tmp_path, monkeypatch):
    for env in _capture_engine_envs(tmp_path, monkeypatch, _MUST_SURVIVE):
        for key, value in _MUST_SURVIVE.items():
            assert env.get(key) == value, f"{key} was dropped from the engine subprocess"
        # We set this ourselves regardless of what the pod's own environment says.
        assert env["TF_IN_AUTOMATION"] == "1"


# --- cache key: bounded by normalisation ----------------------------------
def test_surface_cache_key_normalises_interior_whitespace():
    """Equivalent constraint spellings share one entry.

    `_VERSION_CONSTRAINT_RE` permits `\\s`, so without normalisation a caller can
    mint unboundedly many distinct keys — each a miss that re-downloads a provider.
    """
    canonical = svc._surface_cache_key("tofu", "1.12", "aws", "< 6.0")
    for variant in ("<  6.0", "<\t6.0", "< 6.0 ", " <  6.0", "<\n6.0"):
        assert svc._surface_cache_key("tofu", "1.12", "aws", variant) == canonical
    # Genuinely different constraints still get different keys.
    assert svc._surface_cache_key("tofu", "1.12", "aws", "< 5.0") != canonical


@pytest.mark.asyncio
async def test_create_session_normalises_the_stored_constraint():
    db = AsyncMock()
    db.add = lambda _obj: None  # sync on the real session; AsyncMock would return a coroutine
    session = await svc.create_session(
        db, workspace_id=uuid.uuid4(), provider="aws", created_by="a@b", provider_version="<  6.0"
    )
    assert session.provider_version == "< 6.0"


# --- scratch directories are always reaped --------------------------------
def _fake_download_into(tmp_path):
    """A stand-in for `_download_engine_binary` that leaves a real dir behind."""

    async def _download(_db, engine, _version):
        dest_dir = tempfile.mkdtemp(prefix="onb-bin-", dir=str(tmp_path))
        dest = os.path.join(dest_dir, engine)
        with open(dest, "wb") as f:
            f.write(b"#!/bin/true\n")
        return dest, dest_dir

    return _download


@pytest.mark.asyncio
async def test_run_schema_discovery_reaps_both_scratch_dirs(tmp_path):
    """Nothing is left on the PVC — neither the workdir nor the binary dir.

    `_resolve_tmpdir` falls back to the system default when no PVC is configured,
    which on an API pod is RAM-backed, so a leak here is memory not just disk.
    """
    ws = _workspace()
    session = OnboardingSession(workspace_id=ws.id, provider="aws", status="pending")
    db = _fake_db(session, ws)

    with (
        patch.object(svc, "_resolve_tmpdir", return_value=str(tmp_path)),
        patch.object(svc, "get_cached_surface", AsyncMock(return_value=None)),
        patch.object(svc, "set_cached_surface", AsyncMock()),
        patch.object(svc, "_download_engine_binary", _fake_download_into(tmp_path)),
        patch.object(
            svc, "_discover_surface_blocking", return_value={"count": 0, "data_sources": []}
        ),
    ):
        await svc.run_schema_discovery(db, session.id)

    assert session.status == "schema_ready"
    assert os.listdir(tmp_path) == []


@pytest.mark.asyncio
async def test_run_schema_discovery_reaps_both_scratch_dirs_on_failure(tmp_path):
    ws = _workspace()
    session = OnboardingSession(workspace_id=ws.id, provider="aws", status="pending")
    db = _fake_db(session, ws)

    with (
        patch.object(svc, "_resolve_tmpdir", return_value=str(tmp_path)),
        patch.object(svc, "get_cached_surface", AsyncMock(return_value=None)),
        patch.object(svc, "_download_engine_binary", _fake_download_into(tmp_path)),
        patch.object(svc, "_discover_surface_blocking", side_effect=RuntimeError("init failed")),
    ):
        await svc.run_schema_discovery(db, session.id)

    assert session.status == "errored"
    assert os.listdir(tmp_path) == []


def _tofu_release_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tofu", "#!/bin/true\n")
    return buf.getvalue()


class _FakeStream:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, _size):
        yield self._payload


class _FakeHTTPClient:
    payload = b""

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def stream(self, _method, _url):
        return _FakeStream(type(self).payload)


def _patched_binary_cache(url="https://example.invalid/tofu.zip"):
    return (
        patch(
            "terrapod.services.binary_cache_service.resolve_version",
            AsyncMock(return_value="1.12.0"),
        ),
        patch(
            "terrapod.services.binary_cache_service.get_or_cache_binary",
            AsyncMock(return_value=url),
        ),
        patch("terrapod.storage.get_storage", lambda: None),
    )


@pytest.mark.asyncio
async def test_download_engine_binary_leaves_only_the_binary(tmp_path):
    """The release archive is unlinked once extracted — it is dead weight on the PVC."""
    resolve, cache, storage = _patched_binary_cache()
    _FakeHTTPClient.payload = _tofu_release_zip()
    with (
        patch.object(svc, "_resolve_tmpdir", return_value=str(tmp_path)),
        resolve,
        cache,
        storage,
        patch.object(svc.httpx, "AsyncClient", _FakeHTTPClient),
    ):
        dest, dest_dir = await svc._download_engine_binary(AsyncMock(), "tofu", "1.12")

    assert os.path.basename(dest) == "tofu"
    assert os.listdir(dest_dir) == ["tofu"]  # no leftover tofu.zip


@pytest.mark.asyncio
async def test_download_engine_binary_reaps_its_own_dir_on_failure(tmp_path):
    """A failure here must leave nothing: the caller never receives the dir to reap."""
    resolve, cache, storage = _patched_binary_cache()
    with (
        patch.object(svc, "_resolve_tmpdir", return_value=str(tmp_path)),
        resolve,
        cache,
        storage,
        patch.object(svc.httpx, "AsyncClient", side_effect=RuntimeError("network down")),
        pytest.raises(RuntimeError, match="network down"),
    ):
        await svc._download_engine_binary(AsyncMock(), "tofu", "1.12")

    assert os.listdir(tmp_path) == []
