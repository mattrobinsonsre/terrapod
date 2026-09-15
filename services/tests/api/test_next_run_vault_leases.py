"""The router hooks for Vault lease revocation (#1649).

Drives the real ``next_run``, ``job-launched`` and ``job-status`` handlers
with collaborators mocked at the service boundary. The properties pinned:

- a successful claim records its leases — only with ``revoke_leases`` on, and
  only for a read that returned a lease (a kv-v2 read has none);
- a failed claim — Vault denied a later read, or went away and the run went
  back to the queue (#1646's apply unclaim included) — records nothing;
- recording can never fail or change the claim: Redis down still returns the
  run, with no transition;
- with the option off, the claim makes no Redis call for it;
- the listener's ``terminal`` flag reaches the job-status record;
- no lease id is logged.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.config import VaultConfig, settings
from terrapod.services.variable_service import ResolvedVariable
from terrapod.services.vault_client import (
    VaultDenied,
    VaultLease,
    VaultResponse,
    VaultUnavailable,
)

LEASE_ID = "database/creds/ro/LEASE-ID-MUST-NOT-LEAK"


def _lease(lease_id=LEASE_ID) -> VaultLease:
    return VaultLease(
        duration=1800, renewable=True, received_at=datetime.now(UTC), lease_id=lease_id
    )


def _dyn(key, path="creds/ro", *, vault="db"):
    return ResolvedVariable(
        key=key,
        value=json.dumps(
            {
                "source": "vault",
                "vault": vault,
                "engine": "dynamic",
                "mount": "database",
                "path": path,
                "field": "password",
            }
        ),
        category="env",
        structured=False,
        sensitive=True,
        value_source="vault",
    )


def _config(*, revoke: bool) -> VaultConfig:
    return VaultConfig(
        enabled=True,
        instances=[
            {"name": "db", "address": "https://v", "revoke_leases": revoke, "default": True},
            {"name": "kv", "address": "https://v2"},
        ],
    )


@pytest.fixture(autouse=True)
def _restore_vault():
    prior = settings.vault
    yield
    settings.vault = prior


async def _claim(resolved, read, *, phase="plan", record=None, engine="terraform"):
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.source = "tfe-api"
    ws = MagicMock(var_files=[], working_directory="")
    # The engine seam (#1407): next_run emits the workspace's engine, and for a
    # Pulumi workspace its stack (from `project::stack`) and bind-plan setting.
    ws.engine = engine
    ws.name = "smoke::dev"
    ws.pulumi_bind_plan = False
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    db.add_all = MagicMock()
    transition = AsyncMock()
    record = record or AsyncMock()
    with (
        patch.object(
            runs_router.agent_pool_service,
            "get_listener",
            AsyncMock(return_value={"pool_id": str(uuid.uuid4()), "name": "l"}),
        ),
        patch.object(
            runs_router.run_service, "claim_next_run", AsyncMock(return_value=(run, phase))
        ),
        patch.object(runs_router.run_service, "transition_run", transition),
        patch(
            "terrapod.services.variable_service.resolve_variables",
            AsyncMock(return_value=resolved),
        ),
        patch("terrapod.services.git_auth_service.resolve_git_auth", AsyncMock(return_value=[])),
        patch("terrapod.config.load_runner_config", return_value=MagicMock(hooks_enabled=False)),
        patch.object(
            runs_router, "_run_json", return_value={"data": {"id": "run-x", "attributes": {}}}
        ),
        patch("terrapod.services.vault_source_service.read_secret_response", read),
        patch("terrapod.services.vault_lease_service.record_leases", record),
        patch.object(runs_router, "logger") as api_log,
        patch("terrapod.services.vault_source_service.logger") as vss_log,
    ):
        resp = await runs_router.next_run(
            listener_id=f"listener-{lid}", identity=MagicMock(listener_id=lid), db=db
        )
    return resp, run, transition, record, str(api_log.mock_calls) + str(vss_log.mock_calls)


def _reads(*responses):
    return AsyncMock(side_effect=list(responses))


class TestRecordingAtTheClaim:
    async def test_a_successful_claim_records_its_leases(self):
        settings.vault = _config(revoke=True)
        read = _reads(VaultResponse({"password": "p"}, _lease()))
        resp, run, transition, record, logs = await _claim([_dyn("DB_PASSWORD")], read)

        assert resp.status_code == 200
        transition.assert_not_awaited()
        record.assert_awaited_once()
        run_id, phase, leases = record.await_args.args
        assert (run_id, phase) == (run.id, "plan")
        assert [(name, lease.lease_id) for name, lease in leases] == [("db", LEASE_ID)]
        assert LEASE_ID not in logs

    async def test_the_apply_phase_records_under_apply(self):
        settings.vault = _config(revoke=True)
        read = _reads(VaultResponse({"password": "p"}, _lease()))
        _, _, _, record, _ = await _claim([_dyn("DB_PASSWORD")], read, phase="apply")
        assert record.await_args.args[1] == "apply"

    async def test_a_read_with_no_lease_records_nothing(self):
        settings.vault = _config(revoke=True)
        read = _reads(VaultResponse({"password": "p"}, None))
        resp, _, _, record, _ = await _claim([_dyn("DB_PASSWORD")], read)
        assert resp.status_code == 200
        record.assert_not_awaited()

    async def test_an_instance_without_the_option_records_nothing(self):
        settings.vault = _config(revoke=True)
        read = _reads(VaultResponse({"password": "p"}, _lease()))
        _, _, _, record, _ = await _claim([_dyn("KV_PASSWORD", vault="kv")], read)
        record.assert_not_awaited()

    async def test_option_off_records_nothing_and_touches_no_redis(self):
        settings.vault = _config(revoke=False)
        read = _reads(VaultResponse({"password": "p"}, _lease()))
        # Asserted by call count, not by raising: a raise would be swallowed by
        # the very best-effort handling that must not be reached at all.
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=AssertionError("Redis must not be touched"),
        ) as get_redis:
            from terrapod.services import vault_lease_service

            spy = AsyncMock(wraps=vault_lease_service.record_leases)
            resp, _, _, record, _ = await _claim([_dyn("DB_PASSWORD")], read, record=spy)
        assert resp.status_code == 200
        record.assert_not_awaited()
        get_redis.assert_not_called()

    async def test_option_off_even_a_direct_record_call_touches_no_redis(self):
        # Defence in depth: record_leases gates on the option itself, so a
        # caller that forgot to filter still records nothing.
        settings.vault = _config(revoke=False)
        from terrapod.services import vault_lease_service

        with patch("terrapod.redis.client.get_redis_client") as get_redis:
            await vault_lease_service.record_leases(uuid.uuid4(), "plan", [("db", _lease())])
        get_redis.assert_not_called()


class TestAFailedClaimRecordsNothing:
    async def test_a_denied_later_read(self):
        settings.vault = _config(revoke=True)
        read = _reads(
            VaultResponse({"password": "p"}, _lease()),
            VaultDenied("Vault denied 'database/creds/other'"),
        )
        resp, _, transition, record, _ = await _claim(
            [_dyn("A"), _dyn("B", path="creds/other")], read
        )
        assert resp.status_code == 204
        assert transition.await_args.args[2] == "errored"
        record.assert_not_awaited()

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    async def test_vault_going_away_mid_claim_hands_the_run_back_unrecorded(self, phase):
        # The run is handed back (to the queue; for apply, to `confirmed` once
        # #1646 is on this line).
        # No Job receives these credentials, so nothing of this claim may be
        # revoked — and a later claim's record is never touched by it.
        settings.vault = _config(revoke=True)
        read = _reads(
            VaultResponse({"password": "p"}, _lease()),
            VaultUnavailable("HTTP 503"),
        )
        resp, _, _, record, _ = await _claim(
            [_dyn("A"), _dyn("B", path="creds/other")], read, phase=phase
        )
        assert resp.status_code == 204
        record.assert_not_awaited()


class TestRecordingCannotHurtTheClaim:
    async def test_redis_down_still_returns_the_run(self):
        settings.vault = _config(revoke=True)
        read = _reads(VaultResponse({"password": "p"}, _lease()))
        from terrapod.services import vault_lease_service

        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=ConnectionError(f"redis down {LEASE_ID}"),
        ):
            with patch.object(vault_lease_service, "logger") as lease_log:
                resp, _, transition, _, logs = await _claim(
                    [_dyn("DB_PASSWORD")],
                    read,
                    record=AsyncMock(wraps=vault_lease_service.record_leases),
                )
        assert resp.status_code == 200
        assert json.loads(resp.body)["data"]["attributes"]["env-vars"] == [
            {"key": "DB_PASSWORD", "value": "p"}
        ]
        transition.assert_not_awaited()
        lease_log.warning.assert_called_once()
        assert LEASE_ID not in logs + str(lease_log.mock_calls)


class TestJobLaunched:
    async def _launched(self, run):
        db = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.services.vault_lease_service.record_job", AsyncMock()) as rec,
        ):
            resp = await runs_router.report_job_launched(
                listener_id=f"listener-{run.listener_id}",
                run_id=f"run-{run.id}",
                body={"job_name": "tprun-x-plan", "job_namespace": "runners"},
                identity=MagicMock(listener_id=run.listener_id),
                db=db,
            )
        return resp, rec, db

    async def test_hands_the_job_to_the_lease_record_after_the_commit(self):
        run = MagicMock(id=uuid.uuid4(), listener_id=uuid.uuid4())
        resp, rec, db = await self._launched(run)
        assert resp.status_code == 200
        db.commit.assert_awaited_once()
        rec.assert_awaited_once_with(run, "tprun-x-plan", "runners")

    async def test_option_off_touches_no_redis(self):
        settings.vault = _config(revoke=False)
        run = MagicMock(id=uuid.uuid4(), listener_id=uuid.uuid4(), apply_started_at=None)
        db = AsyncMock()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.get_redis_client") as get_redis,
        ):
            resp = await runs_router.report_job_launched(
                listener_id=f"listener-{run.listener_id}",
                run_id=f"run-{run.id}",
                body={"job_name": "tprun-x-plan", "job_namespace": "runners"},
                identity=MagicMock(listener_id=run.listener_id),
                db=db,
            )
        assert resp.status_code == 200
        get_redis.assert_not_called()


class TestJobStatusCarriesTerminal:
    async def _report(self, body):
        run = MagicMock(id=uuid.uuid4())
        lid = uuid.uuid4()
        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch("terrapod.redis.client.set_job_status", AsyncMock()) as setter,
        ):
            await runs_router.report_job_status(
                listener_id=f"listener-{lid}",
                run_id=f"run-{run.id}",
                body=body,
                identity=MagicMock(listener_id=lid),
                db=AsyncMock(),
            )
        return run, setter

    @pytest.mark.parametrize("terminal", [True, False])
    async def test_the_flag_is_stored(self, terminal):
        run, setter = await self._report(
            {"status": "failed", "phase": "plan", "terminal": terminal}
        )
        setter.assert_awaited_once_with(str(run.id), "plan", "failed", terminal=terminal)

    async def test_a_lagging_listener_stores_none(self):
        run, setter = await self._report({"status": "succeeded", "phase": "apply"})
        setter.assert_awaited_once_with(str(run.id), "apply", "succeeded", terminal=None)

    async def test_a_malformed_flag_is_ignored(self):
        run, setter = await self._report({"status": "failed", "terminal": "yes"})
        assert setter.await_args.kwargs == {"terminal": None}
