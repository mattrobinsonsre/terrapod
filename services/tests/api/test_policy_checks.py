"""The policy-checks router: what the CLI reads and overrides after a plan (#1704).

Handlers are called directly with a mocked session, as in
``test_security_scanning.py``; the check contents themselves are covered in
``tests/services/test_post_plan_decisions.py``.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import policy_checks as router
from terrapod.auth import capabilities as cap
from terrapod.services import policy_check_service
from terrapod.services.policy_check_service import PolicyCheck

STAMP = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)
READ = {cap.RUN_READ}
ADMIN = {cap.RUN_READ, cap.WORKSPACE_SETTINGS}


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="user@example.com",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _setup(status="planning"):
    ws = MagicMock(id=uuid.uuid4())
    run = MagicMock(id=uuid.uuid4(), workspace_id=ws.id, status=status)

    async def _get(model, key):
        from terrapod.db.models import Run, Workspace

        if model is Run and key == run.id:
            return run
        if model is Workspace and key == ws.id:
            return ws
        return None

    db = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    return db, run


def _check(run, kind="opa", status="soft_failed"):
    return PolicyCheck(
        kind=kind,
        run_id=run.id,
        status=status,
        scope="organization" if kind == "opa" else "workspace",
        soft_failed=1 if status == "soft_failed" else 0,
        queued_at=STAMP,
        output="Policy set 'baseline' (mandatory): failed",
    )


def _caps(value):
    return patch.object(router, "resolve_workspace_capabilities_for", AsyncMock(return_value=value))


class TestReading:
    async def test_lists_a_runs_checks_in_the_shape_the_cli_reads(self):
        db, run = _setup()
        request = MagicMock(query_params={})
        with (
            _caps(READ),
            patch.object(
                policy_check_service,
                "list_checks",
                AsyncMock(return_value=[_check(run), _check(run, "scan", "passed")]),
            ),
        ):
            resp = await router.list_policy_checks(
                request, run_id=f"run-{run.id}", user=_user(), db=db
            )
        import json

        body = json.loads(resp.body)
        assert [d["id"] for d in body["data"]] == [
            f"polchk-opa-{run.id}",
            f"polchk-scan-{run.id}",
        ]
        first = body["data"][0]["attributes"]
        assert first["status"] == "soft_failed"
        assert first["actions"] == {"is-overridable": True}
        # A reader who may not override is told so, which is what stops the CLI
        # offering an override it cannot perform.
        assert first["permissions"] == {"can-override": False}
        assert first["status-timestamps"]["soft-failed-at"] == "2026-09-17T12:30:00Z"
        assert body["meta"]["pagination"]["total-count"] == 2

    async def test_output_is_plain_text(self):
        db, run = _setup()
        with (
            _caps(READ),
            patch.object(policy_check_service, "get_check", AsyncMock(return_value=_check(run))),
        ):
            resp = await router.policy_check_output(
                check_id=f"polchk-opa-{run.id}", user=_user(), db=db
            )
        assert resp.media_type == "text/plain"
        assert b"(mandatory): failed" in resp.body

    @pytest.mark.parametrize(
        "check_id",
        ["polchk-opa-not-a-uuid", "pc-123", "polchk-opa-01a0af8c-79a0-759c-86e3-3607f5b419e6"],
    )
    async def test_an_unknown_check_is_404(self, check_id):
        db, _ = _setup()
        with _caps(READ), pytest.raises(HTTPException) as exc:
            await router.show_policy_check(check_id=check_id, user=_user(), db=db)
        assert exc.value.status_code == 404

    async def test_a_caller_who_cannot_read_the_workspace_gets_404_not_403(self):
        db, run = _setup()
        with _caps(set()), pytest.raises(HTTPException) as exc:
            await router.show_policy_check(check_id=f"polchk-opa-{run.id}", user=_user(), db=db)
        assert exc.value.status_code == 404

    async def test_a_gate_that_recorded_nothing_has_no_check(self):
        db, run = _setup()
        with (
            _caps(READ),
            patch.object(policy_check_service, "get_check", AsyncMock(return_value=None)),
            pytest.raises(HTTPException) as exc,
        ):
            await router.show_policy_check(check_id=f"polchk-scan-{run.id}", user=_user(), db=db)
        assert exc.value.status_code == 404


class TestOverriding:
    async def test_overrides_then_moves_the_run_on_at_once(self):
        db, run = _setup()
        after = _check(run, status="overridden")
        with (
            _caps(ADMIN),
            patch.object(
                policy_check_service, "get_check", AsyncMock(side_effect=[_check(run), after])
            ),
            patch.object(policy_check_service, "override_check", AsyncMock(return_value=1)) as ov,
            patch.object(router.run_service, "complete_plan", AsyncMock(return_value=run)) as cp,
        ):
            resp = await router.override_policy_check(
                check_id=f"polchk-opa-{run.id}", user=_user(), db=db
            )
        ov.assert_awaited_once_with(db, run, "opa", "user@example.com")
        cp.assert_awaited_once_with(db, run)
        assert b'"overridden"' in resp.body

    async def test_needs_the_override_permission(self):
        db, run = _setup()
        with (
            _caps(READ),
            patch.object(policy_check_service, "get_check", AsyncMock(return_value=_check(run))),
            patch.object(policy_check_service, "override_check", AsyncMock()) as ov,
            pytest.raises(HTTPException) as exc,
        ):
            await router.override_policy_check(check_id=f"polchk-opa-{run.id}", user=_user(), db=db)
        assert exc.value.status_code == 403
        ov.assert_not_awaited()

    @pytest.mark.parametrize("status", ["passed", "overridden"])
    async def test_only_a_soft_failed_check_can_be_overridden(self, status):
        db, run = _setup()
        with (
            _caps(ADMIN),
            patch.object(
                policy_check_service,
                "get_check",
                AsyncMock(return_value=_check(run, status=status)),
            ),
            patch.object(policy_check_service, "override_check", AsyncMock()) as ov,
            pytest.raises(HTTPException) as exc,
        ):
            await router.override_policy_check(check_id=f"polchk-opa-{run.id}", user=_user(), db=db)
        assert exc.value.status_code == 409
        ov.assert_not_awaited()

    async def test_a_run_no_longer_held_is_not_re_driven(self):
        db, run = _setup(status="discarded")
        with (
            _caps(ADMIN),
            patch.object(
                policy_check_service,
                "get_check",
                AsyncMock(side_effect=[_check(run), _check(run, status="overridden")]),
            ),
            patch.object(policy_check_service, "override_check", AsyncMock(return_value=1)),
            patch.object(router.run_service, "complete_plan", AsyncMock()) as cp,
        ):
            await router.override_policy_check(check_id=f"polchk-opa-{run.id}", user=_user(), db=db)
        cp.assert_not_awaited()


class TestTheOverrideCoversWhatTheCheckCovers:
    async def test_opa_overrides_the_policy_evaluations(self):
        run = MagicMock(id=uuid.uuid4())
        with patch(
            "terrapod.services.policy_set_service.override_run_policies", AsyncMock(return_value=2)
        ) as ov:
            assert await policy_check_service.override_check(AsyncMock(), run, "opa", "a@b") == 2
        ov.assert_awaited_once()

    async def test_scan_overrides_the_scan(self):
        run = MagicMock(id=uuid.uuid4())
        with patch(
            "terrapod.services.security_scan_service.override_run_scan", AsyncMock(return_value=1)
        ) as ov:
            assert await policy_check_service.override_check(AsyncMock(), run, "scan", "a@b") == 1
        ov.assert_awaited_once()
