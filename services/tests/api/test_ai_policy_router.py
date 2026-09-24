"""Router tier for the AI policy gate's two endpoints (#1766).

- ``GET  /runs/{id}/ai-policy``                  (workspace read)
- ``POST /runs/{id}/actions/override-ai-policy`` (workspace admin, re-drive)

The gate blocks applies, so the override is the endpoint that releases a run
somebody's policy deliberately stopped. It shipped with twelve service-layer
tests and **none at this tier**, which is the wrong half to leave uncovered:
the service decides what a verdict means, the router decides who is allowed to
overrule it. Structural twin of ``test_security_scanning.py``, whose override
carries the same contract.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import ai_policy as router
from terrapod.auth import capabilities as cap


def _user(email: str = "user@terrapod") -> AuthenticatedUser:
    return AuthenticatedUser(
        email=email,
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
        run_id=None,
    )


def _ws(**kw):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.name = "smoke"
    m.ai_policy_mode = kw.get("mode", "default")
    return m


def _run(ws, **kw):
    m = MagicMock()
    m.id = kw.get("id", uuid.uuid4())
    m.workspace_id = ws.id
    m.status = kw.get("status", "planning")
    m.plan_only = kw.get("plan_only", False)
    return m


def _mock_db(run, ws):
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


def _row(**kw):
    """Every field `_evaluation_json` reads, spelled out.

    A MagicMock auto-creates whatever is asked of it, so a missing field does
    not raise here — it becomes an unserialisable mock and the failure arrives
    as a TypeError from the JSON encoder, naming the attribute.
    """
    return MagicMock(
        id=uuid.uuid4(),
        enforcement_level=kw.get("enforcement_level", "mandatory"),
        risk_threshold=kw.get("risk_threshold", "high"),
        outcome=kw.get("outcome", "denied"),
        verdict=kw.get("verdict", {"reasons": ["risk above threshold"]}),
        risk_level=kw.get("risk_level", "high"),
        error=None,
        overridden_by=kw.get("overridden_by"),
        overridden_at=None,
        created_at=None,
    )


# ── GET (workspace read) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reading_the_verdict_requires_read_on_the_workspace() -> None:
    ws = _ws()
    run = _run(ws)
    with patch.object(
        router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value=set())
    ):
        with pytest.raises(HTTPException) as exc:
            await router.get_run_ai_policy(
                run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_run_is_404_not_500() -> None:
    ws = _ws()
    run = _run(ws)
    with pytest.raises(HTTPException) as exc:
        await router.get_run_ai_policy(
            run_id=f"run-{uuid.uuid4()}", user=_user(), db=_mock_db(run, ws)
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_run_with_no_verdict_says_why_rather_than_returning_a_bare_null() -> None:
    """A null with no explanation reads as "the gate passed". It does not —
    a mandatory gate holds the run precisely while there is no ruling."""
    ws = _ws()
    run = _run(ws)
    with (
        patch.object(
            router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value={cap.RUN_READ})
        ),
        patch.object(router.ai_policy_service, "get_evaluation", new=AsyncMock(return_value=None)),
        patch.object(
            router.ai_policy_service,
            "effective_enforcement",
            new=MagicMock(return_value="advisory"),
        ),
        patch.object(
            router.ai_policy_service,
            "run_is_held_by_ai_policy",
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = await router.get_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    import json

    body = json.loads(resp.body)
    assert body["data"] is None
    assert body["meta"]["enforcement-level"] == "advisory"
    assert body["meta"]["blocking"] is False


@pytest.mark.asyncio
async def test_the_blocking_flag_comes_from_the_service_not_the_row() -> None:
    """Whether a run is HELD is a live question the service answers; a denied
    verdict on an advisory workspace blocks nothing."""
    ws = _ws()
    run = _run(ws)
    with (
        patch.object(
            router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value={cap.RUN_READ})
        ),
        patch.object(
            router.ai_policy_service, "get_evaluation", new=AsyncMock(return_value=_row())
        ),
        patch.object(
            router.ai_policy_service,
            "effective_enforcement",
            new=MagicMock(return_value="advisory"),
        ),
        patch.object(
            router.ai_policy_service,
            "run_is_held_by_ai_policy",
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = await router.get_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    import json

    body = json.loads(resp.body)
    assert body["data"]["attributes"]["outcome"] == "denied"
    assert body["meta"]["blocking"] is False


@pytest.mark.asyncio
async def test_a_mandatory_hold_with_no_verdict_reports_blocking_true() -> None:
    """The state the live smoke caught (#1822).

    A mandatory gate holds a run while no verdict has landed, and
    `run_service.blocked_by` says so -- but this endpoint built `blocking`
    from the ROW-only predicate, which answers False when there is no row.
    Two endpoints then described the same run differently.

    It is not a cosmetic disagreement: the web panel keys both its blocked
    banner and its override button on `meta.blocking`, so a False here renders
    the quiet "gate is off" styling and hides the only control that releases
    the run -- on a run that is held indefinitely, which is precisely when an
    operator needs it.
    """
    ws = _ws()
    run = _run(ws)
    with (
        patch.object(
            router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value={cap.RUN_READ})
        ),
        # No row at all: the summariser never ran, or failed before settling.
        patch.object(router.ai_policy_service, "get_evaluation", new=AsyncMock(return_value=None)),
        patch.object(
            router.ai_policy_service,
            "effective_enforcement",
            new=MagicMock(return_value="mandatory"),
        ),
        patch.object(
            router.ai_policy_service,
            "run_is_held_by_ai_policy",
            new=AsyncMock(return_value=True),
        ),
    ):
        resp = await router.get_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    import json

    body = json.loads(resp.body)
    assert body["data"] is None
    assert body["meta"]["blocking"] is True, (
        "a held run must report blocking=true even with no verdict recorded, "
        "or the UI hides the override that is the only way out"
    )


# ── POST override (workspace admin) ───────────────────────────────────


@pytest.mark.asyncio
async def test_overriding_requires_admin_not_merely_read() -> None:
    """The gate exists to stop an apply. Read access must not release it."""
    ws = _ws()
    run = _run(ws)
    with patch.object(
        router,
        "resolve_workspace_capabilities_for",
        new=AsyncMock(return_value={cap.RUN_READ}),
    ):
        with pytest.raises(HTTPException) as exc:
            await router.override_run_ai_policy(
                run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_overriding_releases_a_run_held_with_no_verdict_at_all() -> None:
    """The case that most needs releasing, and the one that used to 409.

    A run can be held BECAUSE no verdict landed -- the summariser never ran, or
    raised before settling. Refusing the override there told the operator to
    wait for something that was never coming, while the run kept its workspace
    lock and discard was the only exit.
    """
    ws = _ws()
    run = _run(ws, status="planning")
    recorded = _row(outcome="overridden", overridden_by="user@terrapod")
    with (
        patch.object(
            router,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value={cap.WORKSPACE_SETTINGS}),
        ),
        patch.object(
            router.ai_policy_service, "override", new=AsyncMock(return_value=recorded)
        ) as override,
        patch.object(
            router.ai_policy_service,
            "effective_enforcement",
            new=MagicMock(return_value="mandatory"),
        ),
        patch.object(router.run_service, "complete_plan", new=AsyncMock(return_value=run)),
    ):
        resp = await router.override_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    assert resp.status_code == 200
    # The service is told the enforcement level so the row it writes for a
    # never-ruled run is honest about the gate it was held by.
    assert override.await_args.kwargs["enforcement_level"] == "mandatory"


@pytest.mark.asyncio
async def test_an_override_re_drives_a_run_still_held_in_planning() -> None:
    """Without the re-drive the run sits until the next reconciler tick, so
    the override looks like it did nothing."""
    ws = _ws()
    run = _run(ws, status="planning")
    with (
        patch.object(
            router,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value={cap.WORKSPACE_SETTINGS}),
        ),
        patch.object(
            router.ai_policy_service,
            "override",
            new=AsyncMock(return_value=_row(outcome="overridden", overridden_by="user@terrapod")),
        ),
        patch.object(
            router.run_service, "complete_plan", new=AsyncMock(return_value=run)
        ) as complete,
    ):
        await router.override_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_override_on_a_finished_run_does_not_re_drive_it() -> None:
    """Re-driving a run that has left `planning` would move it backwards."""
    ws = _ws()
    run = _run(ws, status="applied")
    with (
        patch.object(
            router,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value={cap.WORKSPACE_SETTINGS}),
        ),
        patch.object(
            router.ai_policy_service,
            "override",
            new=AsyncMock(return_value=_row(outcome="overridden")),
        ),
        patch.object(router.run_service, "complete_plan", new=AsyncMock()) as complete,
    ):
        await router.override_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(), db=_mock_db(run, ws)
        )

    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_override_records_who_did_it() -> None:
    """An override of a governance gate that does not name its actor is not
    auditable, which is most of the point of allowing one."""
    ws = _ws()
    run = _run(ws, status="applied")
    with (
        patch.object(
            router,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value={cap.WORKSPACE_SETTINGS}),
        ),
        patch.object(
            router.ai_policy_service,
            "override",
            new=AsyncMock(return_value=_row(outcome="overridden")),
        ) as override,
        patch.object(router.run_service, "complete_plan", new=AsyncMock()),
    ):
        await router.override_run_ai_policy(
            run_id=f"run-{run.id}", user=_user(actor := "alice@terrapod"), db=_mock_db(run, ws)
        )

    assert override.await_args.kwargs["actor"] == actor
