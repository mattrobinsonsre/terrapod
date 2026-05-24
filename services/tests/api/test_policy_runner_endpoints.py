"""Tests for the runner-protocol policy endpoints (#343).

The runner uses two endpoints, both authenticated with a runner token
scoped to a single run_id:

- ``GET /api/terrapod/v1/runs/{run_id}/policy-bundle``  → applicable
  sets + Terrapod context; stamps ``runs.policy_bundle_fetched_at``.
- ``POST /api/terrapod/v1/runs/{run_id}/policy-results`` → persists
  evaluation rows via Postgres ON CONFLICT DO NOTHING.

These tests exercise the auth boundary (a leaked token for run A
cannot drive policy state on run B), the bundle shape, the results
validation, and idempotency. Integration tests (real DB through
the FastAPI test client + a runner token) live under ``tests/api/``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
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
        "policy_bundle_fetched_at": None,
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
async def test_bundle_stamps_policy_bundle_fetched_at() -> None:
    """Rolling-upgrade safety (B1): GET stamps the run so the post-plan
    gate can distinguish a pre-#343 runner that never fetched from a
    workspace with no applicable sets."""
    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[]),  # no policy sets in scope
    ):
        await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    assert run.policy_bundle_fetched_at is not None
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_bundle_does_not_re_stamp_already_set() -> None:
    """Second GET (retry) is idempotent — doesn't overwrite the first
    fetch timestamp."""
    earlier = datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)
    run = _run(policy_bundle_fetched_at=earlier)
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[]),
    ):
        await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    assert run.policy_bundle_fetched_at == earlier
    db.commit.assert_not_awaited()


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


# ── B1 — evaluate_post_plan rolling-upgrade safety ───────────────────


@pytest.mark.asyncio
async def test_gate_pre343_runner_writes_synthetic_errored() -> None:
    """If the runner never fetched the bundle but applicable sets
    exist, the gate writes synthetic `errored` evaluations and blocks
    — this is the safety net for a pre-#343 runner image cached on a
    K8s node during a Helm rolling upgrade."""
    from terrapod.services import policy_set_service

    run = _run(policy_bundle_fetched_at=None)
    ws = _ws()
    db = _mock_db_with_run(run, ws)
    # The "any existing rows?" probe — return None on first call (no rows).
    db.execute = AsyncMock(side_effect=[MagicMock(first=lambda: None)])
    db.flush = AsyncMock()

    ps = MagicMock(
        id=uuid.uuid4(),
        name="prod-guardrails",
        enforcement_level="mandatory",
        policies=[],
    )

    captured_rows = []

    async def _capture(_db, rows):
        captured_rows.extend(rows)

    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=AsyncMock(side_effect=_capture),
        run_is_policy_blocked=AsyncMock(return_value=True),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_BLOCKED
    assert len(captured_rows) == 1
    assert captured_rows[0]["outcome"] == "errored"
    assert (
        "pre-#343" in captured_rows[0]["result"]["error"].lower()
        or "did not evaluate" in captured_rows[0]["result"]["error"]
    )


@pytest.mark.asyncio
async def test_gate_pre343_runner_with_no_sets_passes() -> None:
    """Pre-#343 runner + no applicable sets — legitimate pass, no
    synthetic rows written."""
    from terrapod.services import policy_set_service

    run = _run(policy_bundle_fetched_at=None)
    ws = _ws()
    db = _mock_db_with_run(run, ws)

    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[]),
        _insert_evaluations=AsyncMock(),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_PASSED


@pytest.mark.asyncio
async def test_gate_new_runner_fast_path_query_only() -> None:
    """When the runner fetched the bundle, the gate skips the
    rolling-upgrade fallback — pure run_is_policy_blocked query."""
    from terrapod.services import policy_set_service

    stamp = datetime.now(__import__("datetime").timezone.utc) - timedelta(seconds=5)
    run = _run(policy_bundle_fetched_at=stamp)
    ws = _ws()
    db = _mock_db_with_run(run, ws)

    applicable_mock = AsyncMock(return_value=[])  # would be called by slow path
    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=applicable_mock,
        run_is_policy_blocked=AsyncMock(return_value=False),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_PASSED
    applicable_mock.assert_not_called()  # fast path skipped the slow-path probe
