"""Tests for the runner-protocol policy endpoints (#343).

The runner uses two endpoints, both authenticated with a runner token
scoped to a single run_id:

- ``GET /api/terrapod/v1/runs/{run_id}/policy-bundle``  → applicable
  sets + Terrapod context.
- ``POST /api/terrapod/v1/runs/{run_id}/policy-results`` → persists
  evaluation rows via Postgres ON CONFLICT DO NOTHING.

These tests exercise the auth boundary (a leaked token for run A
cannot drive policy state on run B), the bundle shape, the results
validation, and idempotency. Integration tests (real DB through
the FastAPI test client + a runner token) live under ``tests/api/``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import policy_sets as router

# ── _require_runner_for_run via the bundle endpoint ──────────────────


def _user(*, method: str = "runner_token", run_id: str | None = "abc123") -> AuthenticatedUser:
    """Minimal AuthenticatedUser stub for direct handler tests. We pass
    it to the handler functions as the resolved `user` dependency."""
    return AuthenticatedUser(
        email="runner@terrapod",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method=method,
        run_id=run_id,
    )


def _run(**kw):
    run_id = kw.pop("id", uuid.uuid4())
    base = {
        "id": run_id,
        "workspace_id": uuid.uuid4(),
        "plan_only": False,
        "message": "m",
        "source": "tfe-api",
        "is_destroy": False,
    }
    base.update(kw)
    m = MagicMock()
    for k, v in base.items():
        setattr(m, k, v)
    return m


def _ws():
    m = MagicMock()
    m.id = uuid.uuid4()
    m.name = "smoke"
    m.labels = {"env": "prod"}
    return m


def _mock_db_with_run(run, ws):
    # Link the run to the workspace so db.get(Workspace, run.workspace_id)
    # returns ws — without this, the gate sees `ws is None` and short-
    # circuits to GATE_PASSED for the wrong reason.
    run.workspace_id = ws.id
    db = MagicMock()

    async def _get(model, key):
        from terrapod.db.models import Run, Workspace

        if model is Run:
            return run if key == run.id else None
        if model is Workspace:
            return ws if key == ws.id else None
        return None

    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_bundle_rejects_non_runner_token() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    db = _mock_db_with_run(run, ws)
    user = _user(method="api_token", run_id=None)
    with pytest.raises(HTTPException) as exc:
        await router.get_policy_bundle(run_id=f"run-{run.id}", user=user, db=db)
    assert exc.value.status_code == 403
    assert "Runner token required" in exc.value.detail


@pytest.mark.asyncio
async def test_bundle_rejects_token_for_wrong_run() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id="run-different")  # mismatched
    with pytest.raises(HTTPException) as exc:
        await router.get_policy_bundle(run_id=f"run-{run.id}", user=user, db=db)
    assert exc.value.status_code == 403
    assert "Token not scoped to this run" in exc.value.detail


@pytest.mark.asyncio
async def test_bundle_returns_applicable_sets_and_context() -> None:
    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    # MagicMock(name=...) reserves `name` for the mock's repr-name, not
    # as an attribute. Set it after construction so `policy.name` is the
    # actual string the endpoint serialises into the bundle JSON.
    policy = MagicMock(
        id=uuid.uuid4(), rego='package terrapod\ndeny contains x if {false; x := ""}'
    )
    policy.name = "no-public-buckets"
    ps = MagicMock(id=uuid.uuid4(), enforcement_level="mandatory", policies=[policy])
    ps.name = "prod-guardrails"

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[ps]),
    ):
        resp = await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    import json

    body = json.loads(resp.body)
    assert len(body["policy_sets"]) == 1
    assert body["policy_sets"][0]["enforcement_level"] == "mandatory"
    assert body["policy_sets"][0]["policies"][0]["name"] == "no-public-buckets"
    assert body["context"]["workspace"]["name"] == "smoke"


# ── POST /policy-results ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_results_rejects_non_runner_token() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    db = _mock_db_with_run(run, ws)
    user = _user(method="api_token", run_id=None)
    with pytest.raises(HTTPException) as exc:
        await router.post_policy_results(
            run_id=f"run-{run.id}", body={"results": []}, user=user, db=db
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_results_rejects_unknown_outcome() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{uuid.uuid4()}",
                "policy_set_name": "x",
                "enforcement_level": "mandatory",
                "outcome": "bogus",  # not in passed/failed/errored
                "result": {},
            }
        ]
    }
    with pytest.raises(HTTPException) as exc:
        await router.post_policy_results(run_id=run_id, body=body, user=user, db=db)
    assert exc.value.status_code == 422
    assert "outcome" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_results_rejects_unknown_enforcement_level() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{uuid.uuid4()}",
                "policy_set_name": "x",
                "enforcement_level": "informational",  # not in advisory/mandatory
                "outcome": "passed",
                "result": {},
            }
        ]
    }
    with pytest.raises(HTTPException) as exc:
        await router.post_policy_results(run_id=run_id, body=body, user=user, db=db)
    assert exc.value.status_code == 422
    assert "enforcement_level" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_results_rejects_non_list_body() -> None:
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    with pytest.raises(HTTPException) as exc:
        await router.post_policy_results(
            run_id=run_id, body={"results": "not-a-list"}, user=user, db=db
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_results_persists_valid_rows() -> None:
    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)
    ps_id = f"polset-{uuid.uuid4()}"

    body = {
        "results": [
            {
                "policy_set_id": ps_id,
                "policy_set_name": "x",
                "enforcement_level": "mandatory",
                "outcome": "failed",
                "result": {"policies": [{"policy": "p", "passed": False, "violations": ["nope"]}]},
            }
        ]
    }

    captured_rows = []

    async def _capture(_db, rows):
        captured_rows.extend(rows)

    with patch.object(
        router.policy_set_service,
        "_insert_evaluations",
        new=AsyncMock(side_effect=_capture),
    ):
        resp = await router.post_policy_results(run_id=run_id, body=body, user=user, db=db)

    import json

    assert resp.status_code == 201
    assert json.loads(resp.body) == {"recorded": 1}
    assert len(captured_rows) == 1
    assert captured_rows[0]["outcome"] == "failed"
    assert captured_rows[0]["enforcement_level"] == "mandatory"


# ── evaluate_post_plan — gate safety via row-vs-set evidence ────────


def _mk_db_with_recorded_set_ids(run, ws, recorded_ids):
    """Build a mock db where the policy_set_id recorded-rows query
    returns the given ids — sized to the test scenario."""
    db = _mock_db_with_run(run, ws)
    result_mock = MagicMock()
    result_mock.all = lambda: [(pid,) for pid in recorded_ids]
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.mark.asyncio
async def test_gate_missing_mandatory_writes_synthetic_errored() -> None:
    """When an applicable mandatory set has no recorded evaluation row,
    the gate synthesises an errored row and blocks. Safety net is
    row-count-vs-set-count — covers the rolling-upgrade case (an old
    runner image that doesn't know about policy-as-code) and any other
    path that prevents the runner from POSTing for an applicable set."""
    from terrapod.services import policy_set_service

    run = _run()
    ws = _ws()
    db = _mk_db_with_recorded_set_ids(run, ws, [])  # no rows recorded

    ps_id = uuid.uuid4()
    ps = MagicMock(id=ps_id, enforcement_level="mandatory", policies=[])
    ps.name = "prod-guardrails"

    captured = []

    async def _capture(_db, rows):
        captured.extend(rows)

    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=AsyncMock(side_effect=_capture),
        run_is_policy_blocked=AsyncMock(return_value=True),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_BLOCKED
    assert len(captured) == 1
    assert captured[0]["outcome"] == "errored"
    assert captured[0]["policy_set_id"] == ps_id
    assert "did not evaluate" in captured[0]["result"]["error"].lower()


@pytest.mark.asyncio
async def test_gate_missing_advisory_does_not_synthesise() -> None:
    """A missing advisory row is silently dropped — advisory means
    warn-not-block, so a missing advisory has no enforcement effect
    to safeguard, and a ghost errored row would confuse the operator."""
    from terrapod.services import policy_set_service

    run = _run()
    ws = _ws()
    db = _mk_db_with_recorded_set_ids(run, ws, [])

    ps = MagicMock(id=uuid.uuid4(), enforcement_level="advisory", policies=[])
    ps.name = "advisory-set"

    captured = []

    async def _capture(_db, rows):
        captured.extend(rows)

    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=AsyncMock(side_effect=_capture),
        run_is_policy_blocked=AsyncMock(return_value=False),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_PASSED
    assert captured == []  # no synthetic write for advisory


@pytest.mark.asyncio
async def test_gate_no_applicable_sets_passes() -> None:
    """Workspace has no policy sets in scope — legitimate pass, no rows
    written, no run_is_policy_blocked check needed."""
    from terrapod.services import policy_set_service

    run = _run()
    ws = _ws()
    db = _mock_db_with_run(run, ws)

    insert_mock = AsyncMock()
    gate_mock = AsyncMock(return_value=False)
    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[]),
        _insert_evaluations=insert_mock,
        run_is_policy_blocked=gate_mock,
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_PASSED
    insert_mock.assert_not_called()
    # Early-return on empty applicable_sets: no need to consult the gate query.
    gate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gate_all_rows_present_skips_synthesis() -> None:
    """When the runner posted a row for every applicable set, the
    gate is a pure run_is_policy_blocked query — no synthetic writes."""
    from terrapod.services import policy_set_service

    run = _run()
    ws = _ws()
    ps_id = uuid.uuid4()
    db = _mk_db_with_recorded_set_ids(run, ws, [ps_id])

    ps = MagicMock(id=ps_id, enforcement_level="mandatory", policies=[])
    ps.name = "prod-guardrails"

    insert_mock = AsyncMock()
    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=insert_mock,
        run_is_policy_blocked=AsyncMock(return_value=False),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_PASSED
    insert_mock.assert_not_called()


# ── NTH-4: empty policy_set_name rejected ─────────────────────────────


@pytest.mark.asyncio
async def test_results_rejects_empty_policy_set_name() -> None:
    """Empty `policy_set_name` would render as a blank set badge in
    the UI — the runner always fills it, so empty is a contract bug
    and must 422."""
    from fastapi import HTTPException

    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{uuid.uuid4()}",
                "policy_set_name": "   ",  # whitespace-only, strips to empty
                "enforcement_level": "mandatory",
                "outcome": "passed",
                "result": {},
            }
        ]
    }
    with pytest.raises(HTTPException) as exc:
        await router.post_policy_results(run_id=run_id, body=body, user=user, db=db)
    assert exc.value.status_code == 422
    assert "policy_set_name" in exc.value.detail.lower()
