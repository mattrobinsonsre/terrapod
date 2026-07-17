"""Services-tier tests for the AI onboarding polish handler (#824 Phase A).

The model call and Redis budget are mocked; the REAL deterministic applier runs
(that's the point — proving the handler persists a genuinely value-preserving
polish and fails safe otherwise). Covers: disabled no-op, happy path, idempotency,
data-presence guard, budget exhaustion, rejected-polish fallback, and model error.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import OnboardingSession
from terrapod.services import onboarding_ai_service as svc


class _CtxDB:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *a):
        return False


def _db_for(session):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: session))
    db.commit = AsyncMock()
    return db


def _session(**kw):
    s = OnboardingSession(workspace_id=uuid.uuid4(), provider="aws", status="config_ready")
    s.id = uuid.uuid4()
    s.generated_config = 'resource "aws_eip" "eipalloc_x" {\n  domain = "vpc"\n}\n'
    s.import_blocks = 'import {\n  to = aws_eip.eipalloc_x\n  id = "eipalloc-x"\n}\n'
    s.polished_config = None
    s.polished_import_blocks = None
    s.ai_assisted = False
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(svc.settings.ai_onboarding, "enabled", True)
    monkeypatch.setattr(svc.settings.ai_onboarding, "model", "bedrock/test")
    monkeypatch.setattr(svc.settings.ai_onboarding, "max_output_tokens", 4096)


def _patch_common(monkeypatch, session, *, remaining=None):
    db = _db_for(session)
    monkeypatch.setattr(svc, "get_db_session", lambda: _CtxDB(db))
    monkeypatch.setattr(svc, "_budget_remaining", AsyncMock(return_value=remaining))
    charge = AsyncMock()
    monkeypatch.setattr(svc, "_budget_charge", charge)
    return db, charge


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    # Default enabled=False — the handler must not even open a DB session.
    monkeypatch.setattr(svc.settings.ai_onboarding, "enabled", False)
    opener = MagicMock()
    monkeypatch.setattr(svc, "get_db_session", opener)
    await svc.handle_onboarding_polish({"session_id": str(uuid.uuid4())})
    opener.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_persists_polish(monkeypatch, enabled):
    session = _session()
    _db, charge = _patch_common(monkeypatch, session)
    monkeypatch.setattr(
        svc,
        "_call_model",
        AsyncMock(
            return_value=(
                {"resources": [{"address": "aws_eip.eipalloc_x", "new_name": "nat_a"}]},
                100,
                50,
            )
        ),
    )

    await svc.handle_onboarding_polish({"session_id": str(session.id)})

    assert session.ai_assisted is True
    assert session.polished_config is not None
    assert 'resource "aws_eip" "nat_a"' in session.polished_config
    assert 'domain = "vpc"' in session.polished_config  # value preserved
    assert "to = aws_eip.nat_a" in session.polished_import_blocks
    assert 'id = "eipalloc-x"' in session.polished_import_blocks  # id untouched
    charge.assert_awaited_once_with(50)


@pytest.mark.asyncio
async def test_already_polished_is_noop(monkeypatch, enabled):
    session = _session(polished_config="already", ai_assisted=True)
    _patch_common(monkeypatch, session)
    call = AsyncMock()
    monkeypatch.setattr(svc, "_call_model", call)
    await svc.handle_onboarding_polish({"session_id": str(session.id)})
    call.assert_not_awaited()
    assert session.polished_config == "already"


@pytest.mark.asyncio
async def test_no_generated_config_is_noop(monkeypatch, enabled):
    session = _session(generated_config="")
    _patch_common(monkeypatch, session)
    call = AsyncMock()
    monkeypatch.setattr(svc, "_call_model", call)
    await svc.handle_onboarding_polish({"session_id": str(session.id)})
    call.assert_not_awaited()
    assert session.polished_config is None


@pytest.mark.asyncio
async def test_budget_exhausted_skips_model(monkeypatch, enabled):
    session = _session()
    _patch_common(monkeypatch, session, remaining=0)
    call = AsyncMock()
    monkeypatch.setattr(svc, "_call_model", call)
    await svc.handle_onboarding_polish({"session_id": str(session.id)})
    call.assert_not_awaited()
    assert session.polished_config is None
    assert session.ai_assisted is False


@pytest.mark.asyncio
async def test_rejected_polish_keeps_raw_but_charges(monkeypatch, enabled):
    session = _session()
    _db, charge = _patch_common(monkeypatch, session)
    # Model hallucinates an address not in the config → apply_polish raises.
    monkeypatch.setattr(
        svc,
        "_call_model",
        AsyncMock(
            return_value=(
                {"resources": [{"address": "aws_eip.ghost", "new_name": "x"}]},
                100,
                40,
            )
        ),
    )
    await svc.handle_onboarding_polish({"session_id": str(session.id)})
    assert session.polished_config is None  # raw kept
    assert session.ai_assisted is False
    charge.assert_awaited_once_with(40)  # tokens were still spent


@pytest.mark.asyncio
async def test_model_error_keeps_raw_no_charge(monkeypatch, enabled):
    session = _session()
    _db, charge = _patch_common(monkeypatch, session)
    monkeypatch.setattr(svc, "_call_model", AsyncMock(side_effect=RuntimeError("boom")))
    await svc.handle_onboarding_polish({"session_id": str(session.id)})
    assert session.polished_config is None
    assert session.ai_assisted is False
    charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_payload_is_noop(monkeypatch, enabled):
    opener = MagicMock()
    monkeypatch.setattr(svc, "get_db_session", opener)
    await svc.handle_onboarding_polish({})  # no session_id
    opener.assert_not_called()
