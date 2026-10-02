"""Runner-token phase binding and lifetime, at the routes (GHSA-xmrf-hxq9-m59m).

A run has two Jobs and each gets its own token, but the token carried no phase —
so a plan-phase token drove the apply-phase routes and vice versa. A speculative
pull-request plan's own token could post an apply result or upload an apply log,
and the token stayed valid for its whole TTL after the run ended.

Everything here drives a **route** with a **runner-token principal**, because that
is where both defects lived. A test calling `require_runner_for_run` directly
would keep passing if a handler stopped passing its phase, and a runner-reachable
gate that is only ever exercised with a session user is how a gate added here has
broken every agent run before.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer runtok:dummy"}


def _runner(run_id, phase=None):
    """A runner-token principal. `phase=None` is a token minted by a listener
    older than the claim — it must pass every phase."""
    return AuthenticatedUser(
        email="runner",
        display_name="Runner Job",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=str(run_id),
        run_phase=phase,
    )


def _mock_run(run_id, ws_id=None):
    run = MagicMock()
    run.id = run_id
    run.workspace_id = ws_id or uuid.uuid4()
    run.plan_only = False
    run.is_drift_detection = False
    run.has_json_output = True
    return run


def _app(user, db):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return app


#: (method, path suffix, the phase the endpoint belongs to). Derived from the
#: runner's own call sites — `_run_plan_phase` / `_run_apply_phase` in
#: job_entrypoint.py and the Pulumi preview/update branches — not from the names.
PHASED_ROUTES = [
    ("GET", "artifacts/plan-file", "apply"),
    ("GET", "artifacts/lock-file", "apply"),
    ("GET", "artifacts/plan-artifacts", "apply"),
    ("PUT", "artifacts/plan-log", "plan"),
    ("PUT", "artifacts/plan-file", "plan"),
    ("PUT", "artifacts/lock-file", "plan"),
    ("PUT", "artifacts/plan-json-output", "plan"),
    ("PUT", "artifacts/cost-estimate", "plan"),
    ("PUT", "artifacts/plan-artifacts", "plan"),
    ("PUT", "artifacts/apply-log", "apply"),
    ("PUT", "artifacts/state", "apply"),
    ("PUT", "artifacts/pulumi-deployment", "apply"),
    ("POST", "state-diverged", "apply"),
    ("PUT", "artifacts/onboarding-config", "plan"),
    ("PUT", "artifacts/onboarding-imports", "plan"),
    ("POST", "artifacts/onboarding-query-results", "plan"),
]

#: Endpoints both phases genuinely use, so they are deliberately NOT phase-bound.
#: Listed so the absence is a decision a reader can check, not an omission.
UNPHASED_ROUTES = [
    ("GET", "artifacts/config"),
    ("GET", "artifacts/state"),
    ("GET", "artifacts/pulumi-deployment"),
    ("POST", "resource-profile"),
]

OTHER_PHASED_ROUTES = [
    ("POST", "plan-result", "plan"),
    ("POST", "apply-result", "apply"),
    ("GET", "security-scan-config", "plan"),
    ("POST", "security-scan-results", "plan"),
    ("GET", "policy-bundle", "plan"),
    ("POST", "policy-results", "plan"),
]


def _other(phase):
    return "apply" if phase == "plan" else "plan"


async def _call(app, method, run_id, suffix, *, tolerate_app_errors=False):
    """Drive the route.

    `tolerate_app_errors` turns an exception escaping the handler into a 500
    response instead of propagating. The positive tests need it: a handler reached
    with a stub session and a stub body usually blows up somewhere past the gate,
    and that blow-up is not what is being asserted — "did the gate refuse" is.
    The refusal tests leave it off, because a refusal is an `HTTPException` that
    FastAPI turns into a response, so nothing should escape on those paths.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=not tolerate_app_errors)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        url = f"/api/terrapod/v1/runs/run-{run_id}/{suffix}"
        if method == "GET":
            return await c.get(url, headers=_AUTH)
        if method == "PUT":
            return await c.put(url, content=b"{}", headers=_AUTH)
        return await c.post(url, json={}, headers=_AUTH)


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheWrongPhaseIsRefused:
    """403 at the route, before the handler does anything with the body."""

    @pytest.mark.parametrize(("method", "suffix", "phase"), PHASED_ROUTES)
    async def test_run_artifacts_route_refuses_the_other_phase(
        self, _db_mock, _redis_mock, _storage_mock, method, suffix, phase
    ):
        run_id = uuid.uuid4()
        db = AsyncMock()
        db.get.return_value = _mock_run(run_id)
        app = _app(_runner(run_id, phase=_other(phase)), db)

        with patch("terrapod.api.routers.run_artifacts.get_storage") as storage:
            storage.return_value = AsyncMock()
            resp = await _call(app, method, run_id, suffix)

        assert resp.status_code == 403, f"{method} {suffix} accepted a {_other(phase)} token"
        assert phase in resp.json()["detail"]

    @pytest.mark.parametrize(("method", "suffix", "phase"), OTHER_PHASED_ROUTES)
    async def test_result_and_gate_routes_refuse_the_other_phase(
        self, _db_mock, _redis_mock, _storage_mock, method, suffix, phase
    ):
        """plan-result / apply-result and the policy + scan runner protocol. These
        are the routes that *decide* a run's outcome, so a token from the other
        phase reaching them is the enabling primitive the report names."""
        run_id = uuid.uuid4()
        db = AsyncMock()
        db.get.return_value = _mock_run(run_id)
        app = _app(_runner(run_id, phase=_other(phase)), db)

        resp = await _call(app, method, run_id, suffix)
        assert resp.status_code == 403, f"{method} {suffix} accepted a {_other(phase)} token"
        assert phase in resp.json()["detail"]


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheRightPhaseAndAnUnphasedTokenAreNotRefused:
    """Without this the refusals above would also pass if the gate simply
    rejected every runner token — and the skew promise would be unproven."""

    @pytest.mark.parametrize(("method", "suffix", "phase"), PHASED_ROUTES + OTHER_PHASED_ROUTES)
    async def test_the_matching_phase_passes_the_gate(
        self, _db_mock, _redis_mock, _storage_mock, method, suffix, phase
    ):
        run_id = uuid.uuid4()
        db = AsyncMock()
        db.get.return_value = _mock_run(run_id)
        app = _app(_runner(run_id, phase=phase), db)

        with patch("terrapod.api.routers.run_artifacts.get_storage") as storage:
            storage.return_value = AsyncMock()
            resp = await _call(app, method, run_id, suffix, tolerate_app_errors=True)

        # Past the gate is all that is asserted — what the handler then makes of a
        # stub body is another test's business. 403 is the only failure here.
        assert resp.status_code != 403, f"{method} {suffix} refused its own phase"

    @pytest.mark.parametrize(
        ("method", "suffix", "phase"),
        PHASED_ROUTES + OTHER_PHASED_ROUTES,
    )
    async def test_a_token_with_no_phase_claim_passes_every_gate(
        self, _db_mock, _redis_mock, _storage_mock, method, suffix, phase
    ):
        """The version-skew promise. A listener older than the claim mints an
        unphased token; refusing it would break every run on a lagging listener
        image for a defence in depth. Absence is "no claim", not "wrong claim"."""
        run_id = uuid.uuid4()
        db = AsyncMock()
        db.get.return_value = _mock_run(run_id)
        app = _app(_runner(run_id, phase=None), db)

        with patch("terrapod.api.routers.run_artifacts.get_storage") as storage:
            storage.return_value = AsyncMock()
            resp = await _call(app, method, run_id, suffix, tolerate_app_errors=True)

        assert resp.status_code != 403, f"{method} {suffix} refused an unphased token"

    @pytest.mark.parametrize(("method", "suffix"), UNPHASED_ROUTES)
    async def test_a_shared_route_takes_either_phase(
        self, _db_mock, _redis_mock, _storage_mock, method, suffix
    ):
        """These four are reached by both Jobs, so binding one would break a run.
        Asserted rather than left implicit, so a future sweep cannot phase-bind
        them by pattern-matching the others."""
        run_id = uuid.uuid4()
        for phase in ("plan", "apply"):
            db = AsyncMock()
            db.get.return_value = _mock_run(run_id)
            app = _app(_runner(run_id, phase=phase), db)
            with patch("terrapod.api.routers.run_artifacts.get_storage") as storage:
                storage.return_value = AsyncMock()
                resp = await _call(app, method, run_id, suffix, tolerate_app_errors=True)
            assert resp.status_code != 403, f"{method} {suffix} refused a {phase} token"


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheRunStillHasToBeLive:
    """The lifetime half, through the real auth path rather than an override —
    `get_current_user` is where the check has to live for it to cover the binary
    cache and provider mirror as well as the artifact routes."""

    @staticmethod
    def _app_with_real_auth(db):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        return app

    @staticmethod
    def _signing_key_patch():
        return patch(
            "terrapod.auth.runner_tokens.get_token_signing_key",
            return_value=b"k" * 32,
        )

    async def _token(self, run_id, phase="plan"):
        from terrapod.auth.runner_tokens import generate_runner_token

        cfg = MagicMock()
        cfg.max_token_ttl_seconds = 7200
        with (
            self._signing_key_patch(),
            patch("terrapod.config.load_runner_config", return_value=cfg),
        ):
            return generate_runner_token(run_id, ttl=3600, phase=phase)

    async def test_a_token_for_a_terminal_run_is_refused_at_auth(
        self, _db_mock, _redis_mock, _storage_mock
    ):
        """401, not 403: a token whose run has ended is not a credential, so it
        fails authentication rather than authorization."""
        run_id = uuid.uuid4()
        token = await self._token(run_id)

        db = AsyncMock()
        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = "applied"
        db.execute = AsyncMock(return_value=status_result)
        db.get.return_value = _mock_run(run_id)

        app = self._app_with_real_auth(db)
        with (
            self._signing_key_patch(),
            patch(
                "terrapod.redis.client.get_redis_client",
                side_effect=RuntimeError("no redis in this test"),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/terrapod/v1/runs/run-{run_id}/artifacts/config",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 401

    async def test_a_token_for_a_live_run_authenticates(self, _db_mock, _redis_mock, _storage_mock):
        """The other half: the lifetime check must not refuse a running Job."""
        run_id = uuid.uuid4()
        token = await self._token(run_id)

        db = AsyncMock()
        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = "planning"
        db.execute = AsyncMock(return_value=status_result)
        run = _mock_run(run_id)
        run.configuration_version_id = uuid.uuid4()
        db.get.return_value = run

        app = self._app_with_real_auth(db)
        with (
            self._signing_key_patch(),
            patch(
                "terrapod.redis.client.get_redis_client",
                side_effect=RuntimeError("no redis in this test"),
            ),
            patch("terrapod.api.routers.run_artifacts.get_storage") as storage,
        ):
            store = AsyncMock()
            store.exists = AsyncMock(return_value=False)
            storage.return_value = store
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/terrapod/v1/runs/run-{run_id}/artifacts/config",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code != 401

    async def test_a_revocation_marker_refuses_even_while_the_row_says_live(
        self, _db_mock, _redis_mock, _storage_mock
    ):
        """What makes revocation prompt: the marker is written on the terminal
        transition and is believed without reading the row."""
        run_id = uuid.uuid4()
        token = await self._token(run_id)

        redis = MagicMock()
        redis.get = AsyncMock(return_value=b"revoked")
        redis.set = AsyncMock()

        db = AsyncMock()
        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = "planning"
        db.execute = AsyncMock(return_value=status_result)
        db.get.return_value = _mock_run(run_id)

        app = self._app_with_real_auth(db)
        with (
            self._signing_key_patch(),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/terrapod/v1/runs/run-{run_id}/artifacts/config",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert resp.status_code == 401
