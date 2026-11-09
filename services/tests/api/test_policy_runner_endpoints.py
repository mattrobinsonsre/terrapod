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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import policy_sets as router

# ── _require_runner_for_run via the bundle endpoint ──────────────────


def _user(
    *,
    method: str = "runner_token",
    run_id: str | None = "abc123",
    run_phase: str | None = None,
) -> AuthenticatedUser:
    """Minimal AuthenticatedUser stub for direct handler tests. We pass
    it to the handler functions as the resolved `user` dependency."""
    return AuthenticatedUser(
        email="runner@terrapod",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method=method,
        run_id=run_id,
        run_phase=run_phase,
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
        # Must be stated: this builds a MagicMock from `base`, so a key left
        # out is a TRUTHY mock rather than an error. The pair is the run's
        # shape, and `(False, True)` is not a legal row -- the three legal ones
        # are ordinary, standalone configure, and composite.
        "is_configure": False,
        "configure_after_apply": False,
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


def _policy_set(set_id, name="Production guardrails", enforcement="mandatory"):
    """A stand-in for the stored definition of a set.

    `SimpleNamespace`, not `MagicMock`: `.name` on a mock is the mock's own
    name attribute, so a mock would silently satisfy an assertion about the
    recorded name without ever carrying the value under test.
    """
    return SimpleNamespace(id=set_id, name=name, enforcement_level=enforcement)


def _mock_db_with_run(run, ws, policy_sets=None):
    # Link the run to the workspace so db.get(Workspace, run.workspace_id)
    # returns ws — without this, the gate sees `ws is None` and short-
    # circuits to GATE_PASSED for the wrong reason.
    run.workspace_id = ws.id
    db = MagicMock()
    sets_by_id = {ps.id: ps for ps in (policy_sets or [])}

    async def _get(model, key):
        from terrapod.db.models import PolicySet, Run, Workspace

        if model is Run:
            return run if key == run.id else None
        if model is Workspace:
            return ws if key == ws.id else None
        if model is PolicySet:
            return sets_by_id.get(key)
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
    ps = MagicMock(
        id=uuid.uuid4(),
        enforcement_level="mandatory",
        policies=[policy],
        shared_evaluation=False,
        support_files={},
    )
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


@pytest.mark.parametrize("token_phase", ["plan", "configure"])
@pytest.mark.asyncio
async def test_the_bundle_context_carries_the_token_s_phase(token_phase: str) -> None:
    """Driven through the ROUTE, because the phase is the router's to supply.

    `build_run_context` has its own unit test, and it cannot catch this: the
    router could pass None, or the run's own shape, and that test would still
    pass. The mutation this exists for is `build_run_context(ws, run, None)`,
    which compiles and leaves every other assertion in this file green.

    A COMPOSITE run (apply-then-configure) is one Run whose plan Job and
    configure Job both fetch this bundle, so the phase is the only field that
    distinguishes them -- a policy guarded on it is otherwise guarded on
    nothing.
    """
    run = _run(is_configure=True, configure_after_apply=True)
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id, run_phase=token_phase)

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[]),
    ):
        resp = await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    import json

    body = json.loads(resp.body)
    assert body["context"]["run"]["phase"] == token_phase


@pytest.mark.parametrize(
    ("token_phase", "allowed"),
    [("plan", True), ("configure", True), ("apply", False), (None, True)],
)
def test_the_policy_endpoints_admit_the_plan_and_configure_phases(
    token_phase: str | None, allowed: bool
) -> None:
    """The guard both policy endpoints call, driven directly.

    They asked for `phase="plan"` alone, and a configure Job's token carries
    `phase="configure"` -- so `fetch_policy_bundle` 403'd three times, raised,
    and `job_entrypoint` returned 1. EVERY configure run failed, on every
    workspace, whether or not a policy set existed, because the 403 happens
    before the "no applicable sets" short-circuit ever runs.

    The apply case is the one that must stay refused: the fix is not
    `phase=None`, which would hand the apply phase a route it has no business
    on and give back GHSA-xmrf-hxq9-m59m. The `None` case is a listener older
    than the phase claim and passes by design.
    """
    from fastapi import HTTPException

    from terrapod.api.dependencies import AuthenticatedUser, require_runner_for_run

    kw: dict[str, object] = {
        "email": "runner",
        "display_name": None,
        "roles": ["everyone"],
        "provider_name": "runner",
        "auth_method": "runner_token",
        "run_id": "abc",
    }
    if token_phase is not None:
        kw["run_phase"] = token_phase
    user = AuthenticatedUser(**kw)

    if allowed:
        require_runner_for_run(user, "abc", phase=("plan", "configure"))
    else:
        with pytest.raises(HTTPException) as exc:
            require_runner_for_run(user, "abc", phase=("plan", "configure"))
        assert exc.value.status_code == 403


def test_the_two_policy_endpoints_ask_for_both_phases() -> None:
    """Both of them, derived from the source rather than listed.

    `get_policy_bundle` and `post_policy_results` are driven by the plan Job
    and the configure Job alike, and fixing only the first leaves the second
    403 behind it -- the runner would fetch a bundle, evaluate it, and then be
    unable to post what it found.
    """
    import ast
    import pathlib as _pathlib

    from terrapod.api.routers import policy_sets as ps_router

    tree = ast.parse(_pathlib.Path(ps_router.__file__).read_text())
    asked: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name not in ("get_policy_bundle", "post_policy_results"):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and getattr(call.func, "id", "") == "require_runner_for_run"
            ):
                kw = {k.arg: k.value for k in call.keywords}
                phase = kw.get("phase")
                asked[node.name] = tuple(ast.literal_eval(phase)) if phase is not None else None

    assert set(asked) == {"get_policy_bundle", "post_policy_results"}, asked
    for name, phases in asked.items():
        assert phases is not None, f"{name} dropped its phase guard entirely"
        assert set(phases) == {"plan", "configure"}, f"{name} asks for {phases}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "shape"),
    [
        ("standalone configure", {"is_configure": True, "configure_after_apply": False}),
        ("composite", {"is_configure": True, "configure_after_apply": True}),
    ],
)
async def test_bundle_is_not_emptied_for_a_configure(label: str, shape: dict) -> None:
    """A configure's bundle carries its sets -- the regression that shipped.

    #2435 put the configure policy gate IN the Job, and the Job's only view of
    which sets to evaluate is this endpoint. It emptied the list whenever
    `policy_sets_not_evaluated_reason` was truthy, and that reason was truthy
    for any run with `is_configure` -- so every configure fetched an EMPTY
    bundle, the gate logged "no applicable policy sets" and evaluated nothing.
    A mandatory set scoped to a configure-enabled workspace was unenforced, and
    the gate reported success having done nothing.

    Driven through the ENDPOINT rather than through the predicate, because the
    predicate was only ever half the defect: the feature's own tests supply
    sets to the gate directly, so they prove the gate works GIVEN a bundle and
    say nothing about whether it is given one. Both run shapes are here because
    the composite reached a second bug through the same reason -- see
    `test_policy_set_service.TestWhyNoPolicySetWasEvaluated`.
    """
    run = _run(**shape)
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    policy = MagicMock(id=uuid.uuid4(), rego="package terrapod")
    policy.name = "no-production-hosts"
    ps = MagicMock(
        id=uuid.uuid4(),
        enforcement_level="mandatory",
        policies=[policy],
        shared_evaluation=False,
        support_files={},
    )
    ps.name = "configure-guardrails"

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[ps]),
    ):
        resp = await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    import json

    body = json.loads(resp.body)
    assert len(body["policy_sets"]) == 1, f"{label}: the runner was handed an EMPTY bundle"
    assert body["policy_sets"][0]["policies"][0]["name"] == "no-production-hosts"


@pytest.mark.asyncio
async def test_bundle_is_still_emptied_for_an_engine_with_no_plan_json() -> None:
    """The emptying that is CORRECT, so the fix cannot have removed both.

    A runner for an engine that produces no plan JSON is given nothing to
    evaluate, matching the post-plan gate, which does not wait for it (#1567).
    Paired with the test above deliberately: a fix that stopped emptying the
    bundle at all would pass that one and break this.
    """
    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    with (
        patch("terrapod.engines.evaluates_policy_sets", return_value=False),
        patch.object(
            router.policy_set_service,
            "applicable_policy_sets",
            new=AsyncMock(return_value=[MagicMock()]),
        ),
    ):
        resp = await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    import json

    assert json.loads(resp.body)["policy_sets"] == []


@pytest.mark.asyncio
async def test_bundle_carries_shared_evaluation_and_support_files() -> None:
    """The bundle is the runner's only view of a set (#1842).

    The runner decides how to evaluate from what the bundle says, so a set
    whose flag or data files do not reach it silently evaluates the old way —
    the operator turns shared evaluation on and nothing changes.
    """
    run = _run()
    ws = _ws()
    run_id = f"run-{run.id}"
    db = _mock_db_with_run(run, ws)
    user = _user(method="runner_token", run_id=run_id)

    policy = MagicMock(id=uuid.uuid4(), rego="package terrapod\ndeny contains x if {false}")
    policy.name = "cidr-allowlist"
    ps = MagicMock(
        id=uuid.uuid4(),
        enforcement_level="mandatory",
        policies=[policy],
        shared_evaluation=True,
        support_files={"data.yaml": "approved_cidrs: []", "helpers.rego": "package terrapod"},
    )
    ps.name = "network"

    with patch.object(
        router.policy_set_service,
        "applicable_policy_sets",
        new=AsyncMock(return_value=[ps]),
    ):
        resp = await router.get_policy_bundle(run_id=run_id, user=user, db=db)

    import json

    entry = json.loads(resp.body)["policy_sets"][0]
    assert entry["shared_evaluation"] is True
    assert entry["support_files"] == {
        "data.yaml": "approved_cidrs: []",
        "helpers.rego": "package terrapod",
    }


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
    ps_uuid = uuid.uuid4()
    db = _mock_db_with_run(run, ws, policy_sets=[_policy_set(ps_uuid)])
    user = _user(method="runner_token", run_id=run_id)
    ps_id = f"polset-{ps_uuid}"

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
    # `blocked` is the API's own verdict, carried back so the configure gate
    # can act on it without restating the rule (#2435). Asserted explicitly
    # rather than loosened to a subset check: the runner refuses the run on
    # this field, so a change to its name or shape must fail here.
    assert json.loads(resp.body) == {"recorded": 1, "blocked": True}
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
async def test_gate_does_not_short_circuit_for_a_composite_run() -> None:
    """A composite's APPLY half is still policy-gated -- the second regression.

    `evaluate_post_plan` short-circuits to `GATE_PASSED` on a
    `policy_sets_not_evaluated_reason`, and that reason was truthy for any run
    carrying `is_configure`. A COMPOSITE (apply-then-configure) carries it
    while genuinely having a plan and an apply and reaching `_complete_plan`,
    so the safety net was skipped on every composite apply: a mandatory set
    with no recorded evaluation passed instead of blocking.

    The function's own comment asserted this path was unreachable -- true of a
    STANDALONE configure, which never reaches `_complete_plan`, and false of a
    composite. Driven here rather than argued, which is why this asserts
    `GATE_BLOCKED` on exactly the fixture the sibling test uses for an ordinary
    run: the only difference is the run shape.
    """
    from terrapod.services import policy_set_service

    run = _run(is_configure=True, configure_after_apply=True)
    ws = _ws()
    db = _mk_db_with_recorded_set_ids(run, ws, [])

    ps = MagicMock(id=uuid.uuid4(), enforcement_level="mandatory", policies=[])
    ps.name = "prod-guardrails"

    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=AsyncMock(),
        run_is_policy_blocked=AsyncMock(return_value=True),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)

    assert result == policy_set_service.GATE_BLOCKED, (
        "a composite's apply skipped the post-plan safety net"
    )


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


# ── what a set IS is server-known, not runner-reported ──────────────


@pytest.mark.asyncio
async def test_the_enforcement_level_comes_from_the_database_not_the_body() -> None:
    """`run_is_policy_blocked` filters on mandatory rows and nothing else, so a
    body claiming `advisory` for a mandatory set lifts it out of the gate — and
    the real evaluation that followed would be dropped by the ON CONFLICT."""
    run, ws = _run(), _ws()
    run_id = f"run-{run.id}"
    ps_uuid = uuid.uuid4()
    db = _mock_db_with_run(run, ws, policy_sets=[_policy_set(ps_uuid, enforcement="mandatory")])

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{ps_uuid}",
                "policy_set_name": "x",
                "enforcement_level": "advisory",
                "outcome": "passed",
                "result": {},
            }
        ]
    }

    captured: list[dict] = []

    async def _capture(_db, rows):
        captured.extend(rows)

    with patch.object(
        router.policy_set_service, "_insert_evaluations", new=AsyncMock(side_effect=_capture)
    ):
        resp = await router.post_policy_results(
            run_id=run_id, body=body, user=_user(method="runner_token", run_id=run_id), db=db
        )

    assert resp.status_code == 201
    assert captured[0]["enforcement_level"] == "mandatory", (
        "the runner's claim of 'advisory' was stored, which removes the set from the gate"
    )


@pytest.mark.asyncio
async def test_the_recorded_name_comes_from_the_database_not_the_body() -> None:
    """The name is what the Policy Checks panel shows an operator deciding
    whether to override. A runner-supplied one can name a different set."""
    run, ws = _run(), _ws()
    run_id = f"run-{run.id}"
    ps_uuid = uuid.uuid4()
    db = _mock_db_with_run(
        run, ws, policy_sets=[_policy_set(ps_uuid, name="Production guardrails")]
    )

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{ps_uuid}",
                "policy_set_name": "Harmless looking set",
                "enforcement_level": "mandatory",
                "outcome": "passed",
                "result": {},
            }
        ]
    }

    captured: list[dict] = []

    async def _capture(_db, rows):
        captured.extend(rows)

    with patch.object(
        router.policy_set_service, "_insert_evaluations", new=AsyncMock(side_effect=_capture)
    ):
        await router.post_policy_results(
            run_id=run_id, body=body, user=_user(method="runner_token", run_id=run_id), db=db
        )

    assert captured[0]["policy_set_name"] == "Production guardrails"


@pytest.mark.asyncio
async def test_a_result_for_an_unknown_set_is_skipped_and_the_rest_survive() -> None:
    """Deliberately not a 422. Failing the batch would discard the results for
    every other set in it, and the gate's safety net would then record those as
    missing mandatory evaluations and block the run — a forged row for a
    non-existent set would become a denial of service."""
    run, ws = _run(), _ws()
    run_id = f"run-{run.id}"
    known = uuid.uuid4()
    db = _mock_db_with_run(run, ws, policy_sets=[_policy_set(known)])

    body = {
        "results": [
            {
                "policy_set_id": f"polset-{uuid.uuid4()}",
                "policy_set_name": "invented",
                "enforcement_level": "mandatory",
                "outcome": "passed",
                "result": {},
            },
            {
                "policy_set_id": f"polset-{known}",
                "policy_set_name": "x",
                "enforcement_level": "mandatory",
                "outcome": "failed",
                "result": {},
            },
        ]
    }

    captured: list[dict] = []

    async def _capture(_db, rows):
        captured.extend(rows)

    with patch.object(
        router.policy_set_service, "_insert_evaluations", new=AsyncMock(side_effect=_capture)
    ):
        resp = await router.post_policy_results(
            run_id=run_id, body=body, user=_user(method="runner_token", run_id=run_id), db=db
        )

    assert resp.status_code == 201
    assert len(captured) == 1
    assert captured[0]["policy_set_id"] == known
    assert captured[0]["outcome"] == "failed"
