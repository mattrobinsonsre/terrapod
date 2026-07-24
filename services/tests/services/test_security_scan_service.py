"""Tests for security_scan_service — config resolution + the post-plan gate (#1036).

The deterministic security-scan gate is the structural twin of the policy gate;
these cover config resolution, the enabled check, the summary shape, and the
gate's decision paths (plan-only / off / advisory never block; enforced blocks
on a recorded failed/errored result and via the missing-result safety net;
override releases).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from terrapod.services import security_scan_service as svc


def _ws(**kw):
    base = {
        "id": uuid.uuid4(),
        "security_scan_enforcement": "off",
        "security_scan_engine": "checkov",
        "security_scan_severity_threshold": "high",
        "security_scan_skip_rules": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _run(plan_only=False, workspace_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(), plan_only=plan_only, workspace_id=workspace_id or uuid.uuid4()
    )


# ── pure config resolution ────────────────────────────────────────────────


def test_scan_enabled():
    assert svc.scan_enabled(_ws(security_scan_enforcement="advisory")) is True
    assert svc.scan_enabled(_ws(security_scan_enforcement="enforced")) is True
    assert svc.scan_enabled(_ws(security_scan_enforcement="off")) is False


def test_resolve_scan_config_off_disables():
    cfg = svc.resolve_scan_config(_ws(security_scan_enforcement="off"))
    assert cfg["enabled"] is False
    assert cfg["enforcement_level"] == "off"


def test_resolve_scan_config_passes_through_engine_threshold_skip():
    cfg = svc.resolve_scan_config(
        _ws(
            security_scan_enforcement="enforced",
            security_scan_engine="both",
            security_scan_severity_threshold="critical",
            security_scan_skip_rules=["CKV_AWS_24", "AVD-AWS-0107"],
        )
    )
    assert cfg == {
        "enabled": True,
        "enforcement_level": "enforced",
        "engine": "both",
        "severity_threshold": "critical",
        "skip_rules": ["CKV_AWS_24", "AVD-AWS-0107"],
    }


# ── gate: the never-block paths ───────────────────────────────────────────


async def test_plan_only_run_never_gated():
    db = AsyncMock()
    assert await svc.evaluate_post_plan(db, _run(plan_only=True)) == svc.GATE_PASSED
    db.get.assert_not_called()  # short-circuits before any DB access


async def test_off_workspace_never_gated():
    ws = _ws(security_scan_enforcement="off")
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    assert await svc.evaluate_post_plan(db, _run(workspace_id=ws.id)) == svc.GATE_PASSED


async def test_advisory_workspace_never_gated():
    ws = _ws(security_scan_enforcement="advisory")
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    assert await svc.evaluate_post_plan(db, _run(workspace_id=ws.id)) == svc.GATE_PASSED


# ── gate: enforced ───────────────────────────────────────────────────────


async def test_enforced_missing_result_synthesizes_errored_and_blocks():
    """A enforced scan with NO runner result must fail closed: write a synthetic
    errored result and block."""
    ws = _ws(security_scan_enforcement="enforced")
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    # 1st execute: existing-check → none; 2nd (record insert); 3rd: blocked → a row
    existing = MagicMock(first=MagicMock(return_value=None))
    blocked = MagicMock(first=MagicMock(return_value=(uuid.uuid4(),)))
    db.execute = AsyncMock(side_effect=[existing, MagicMock(), blocked])
    result = await svc.evaluate_post_plan(db, _run(workspace_id=ws.id))
    assert result == svc.GATE_BLOCKED
    # the synthetic insert + the flush happened
    assert db.execute.await_count >= 2
    db.flush.assert_awaited()


async def test_enforced_with_passed_result_is_not_blocked():
    ws = _ws(security_scan_enforcement="enforced")
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    existing = MagicMock(first=MagicMock(return_value=(uuid.uuid4(),)))  # a result exists
    not_blocked = MagicMock(first=MagicMock(return_value=None))  # no blocking row
    db.execute = AsyncMock(side_effect=[existing, not_blocked])
    assert await svc.evaluate_post_plan(db, _run(workspace_id=ws.id)) == svc.GATE_PASSED


async def test_run_is_scan_blocked_queries_enforced_unoverridden():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(uuid.uuid4(),))))
    assert await svc.run_is_scan_blocked(db, uuid.uuid4()) is True
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    assert await svc.run_is_scan_blocked(db, uuid.uuid4()) is False


# ── summary + override ────────────────────────────────────────────────────


async def test_run_scan_summary_none_when_no_scan(monkeypatch):
    async def _no_scan(_db, _rid):
        return None

    monkeypatch.setattr(svc, "get_run_scan", _no_scan)
    assert await svc.run_scan_summary(AsyncMock(), uuid.uuid4()) is None


async def test_run_scan_summary_blocked_status(monkeypatch):
    scan = SimpleNamespace(outcome="failed", engine="checkov", summary={"total": 3, "blocking": 2})

    async def _scan(_db, _rid):
        return scan

    async def _blocked(_db, _rid):
        return True

    monkeypatch.setattr(svc, "get_run_scan", _scan)
    monkeypatch.setattr(svc, "run_is_scan_blocked", _blocked)
    out = await svc.run_scan_summary(AsyncMock(), uuid.uuid4())
    assert out == {
        "status": "blocked",
        "outcome": "failed",
        "engine": "checkov",
        "total": 3,
        "blocking": 2,
    }


async def test_override_marks_failed_scan(monkeypatch):
    scan = SimpleNamespace(outcome="failed", overridden_by=None, overridden_at=None)

    async def _scan(_db, _rid):
        return scan

    monkeypatch.setattr(svc, "get_run_scan", _scan)
    count = await svc.override_run_scan(AsyncMock(), uuid.uuid4(), "admin@x.io")
    assert count == 1
    assert scan.overridden_by == "admin@x.io"
    assert scan.overridden_at is not None


async def test_override_noop_on_passed_scan(monkeypatch):
    scan = SimpleNamespace(outcome="passed", overridden_by=None, overridden_at=None)

    async def _scan(_db, _rid):
        return scan

    monkeypatch.setattr(svc, "get_run_scan", _scan)
    assert await svc.override_run_scan(AsyncMock(), uuid.uuid4(), "admin@x.io") == 0
    assert scan.overridden_by is None
