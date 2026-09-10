"""The run-artifact endpoints a Pulumi agent run hands its stack over through (#1576).

Agent runs keep the stack in a file backend inside the Job and never use
Terrapod as a live Pulumi backend. These two endpoints are the whole exchange:
the deployment comes in with its secrets opened, and goes back once after an
update, to be sealed and stored as the next state version.
"""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.services.pulumi_state_service import SECRET_SIG, SECRET_SIG_KEY

pytestmark = pytest.mark.asyncio

_BASE = "http://test"
_MOD = "terrapod.api.routers.run_artifacts"
SIG = {SECRET_SIG_KEY: SECRET_SIG}


class _Crypto:
    """A stand-in for the encryption service that makes sealing visible."""

    @staticmethod
    def encrypt(value: str) -> str:
        return f"sealed({value})"

    @staticmethod
    def decrypt(value: str) -> str:
        return value[len("sealed(") : -1]


def _cipher(plaintext: str) -> dict:
    sealed = _Crypto.encrypt(plaintext)
    return {**SIG, "ciphertext": base64.b64encode(sealed.encode()).decode()}


def _runner(run_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="runner",
        display_name="Runner Job",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=str(run_id),
    )


def _run(*, plan_only: bool = False) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.created_by = "dev@example.com"
    run.plan_only = plan_only
    return run


def _ws(run: MagicMock, engine: str = "pulumi") -> MagicMock:
    ws = MagicMock()
    ws.id = run.workspace_id
    ws.name = "proj::dev"
    ws.engine = engine
    ws.state_diverged = False
    return ws


def _sv(serial: int, run_id: uuid.UUID | None = None) -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.serial = serial
    sv.run_id = run_id
    return sv


def _db(run: MagicMock, ws: MagicMock, latest: MagicMock | None) -> AsyncMock:
    db = AsyncMock()
    db.get.side_effect = lambda model, _id: {Run: run, Workspace: ws}.get(model)
    result = MagicMock()
    result.scalar_one_or_none.return_value = latest
    db.execute.return_value = result
    db.add = MagicMock()
    return db


STORED = {
    "manifest": {"time": "t"},
    "secrets_providers": {
        "type": "service",
        "state": {"url": "https://local.example/api/v1/pulumi", "owner": "default"},
    },
    "resources": [{"urn": "urn:a", "outputs": {"pw": _cipher('"hunter2"')}}],
}


class _Harness:
    def __init__(self, run, ws, latest, stored: dict | None = None) -> None:
        self.run, self.ws = run, ws
        self.db = _db(run, ws, latest)
        self.storage = MagicMock()
        self.storage.get = AsyncMock(return_value=json.dumps(stored).encode() if stored else b"")
        self.storage.put = AsyncMock()
        self.discard = AsyncMock()
        self.publish = AsyncMock()

    def __enter__(self) -> _Harness:
        self._stack = ExitStack()
        # The routes are mounted only with the Pulumi engine on (#1429).
        self._stack.enter_context(patch.object(settings.engines.pulumi, "enabled", True))
        for target in (
            patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
            patch("terrapod.api.app.init_redis"),
            patch("terrapod.api.app.init_db"),
            patch(f"{_MOD}.get_storage", return_value=self.storage),
            patch("terrapod.crypto.service.get_encryption", return_value=_Crypto()),
            patch("terrapod.crypto.state.decrypt_state_bytes", AsyncMock(side_effect=lambda b: b)),
            patch("terrapod.crypto.state.encrypt_state_bytes", AsyncMock(side_effect=lambda b: b)),
            patch(
                "terrapod.services.run_service.discard_stale_plans_for_state_change", self.discard
            ),
            patch("terrapod.redis.client.publish_workspace_event", self.publish),
        ):
            self._stack.enter_context(target)
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: _runner(self.run.id)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url=_BASE)
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()

    def url(self, suffix: str = "") -> str:
        return f"/api/v1/runs/{self.run.id}/artifacts/pulumi-deployment{suffix}"


class TestDownload:
    async def test_a_new_stack_answers_null_not_404(self) -> None:
        """For a runner, a failed download read as "no state" would propose
        creating every resource that already exists — so emptiness is a 200."""
        run = _run()
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.get(h.url())
        assert resp.status_code == 200
        assert resp.json() == {"version": 3, "deployment": None}
        assert resp.headers["x-terrapod-state-serial"] == "0"

    async def test_the_stack_arrives_with_its_secrets_open(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=_sv(4), stored=STORED) as h:
            resp = await h.client.get(h.url())
        assert resp.status_code == 200
        deployment = resp.json()["deployment"]
        assert deployment["resources"][0]["outputs"]["pw"] == {**SIG, "plaintext": '"hunter2"'}
        assert "secrets_providers" not in deployment
        assert resp.headers["x-terrapod-state-serial"] == "4"

    async def test_a_stack_sealed_by_another_provider_is_refused(self) -> None:
        run = _run()
        stored = {**STORED, "secrets_providers": {"type": "awskms", "state": {}}}
        with _Harness(run, _ws(run), latest=_sv(1), stored=stored) as h:
            resp = await h.client.get(h.url())
        assert resp.status_code == 409
        assert "awskms" in resp.text

    async def test_a_terraform_workspace_has_no_deployment(self) -> None:
        run = _run()
        with _Harness(run, _ws(run, engine="terraform"), latest=None) as h:
            resp = await h.client.get(h.url())
        assert resp.status_code == 404


def _export(**outputs) -> bytes:
    return json.dumps(
        {
            "version": 3,
            "deployment": {
                "manifest": {"time": "t2"},
                "secrets_providers": {"type": "passphrase", "state": {"salt": "v1:runner"}},
                "resources": [{"urn": "urn:a", "outputs": outputs}],
            },
        }
    ).encode()


def _stored_payload(h: _Harness) -> dict:
    return json.loads(h.storage.put.call_args[0][1])


class TestUpload:
    async def test_the_stack_is_sealed_and_stored_as_the_next_version(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=_sv(4), stored=STORED) as h:
            resp = await h.client.put(
                h.url("?base-serial=4"),
                content=_export(pw={**SIG, "plaintext": '"hunter3"'}),
            )
        assert resp.status_code == 204
        sv = h.db.add.call_args[0][0]
        assert isinstance(sv, StateVersion)
        assert (sv.serial, sv.run_id) == (5, run.id)
        stored = _stored_payload(h)
        assert stored["resources"][0]["outputs"]["pw"] == _cipher('"hunter3"')
        assert "hunter3" not in h.storage.put.call_args[0][1].decode().replace("sealed(", "")
        # The provider the stack already had is kept, not the runner's passphrase.
        assert stored["secrets_providers"] == STORED["secrets_providers"]
        h.discard.assert_awaited_once()
        h.publish.assert_awaited_once()

    async def test_a_first_state_names_this_deployments_service(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.put(h.url("?base-serial=0"), content=_export(x=1))
        assert resp.status_code == 204
        provider = _stored_payload(h)["secrets_providers"]
        assert provider["type"] == "service"
        assert provider["state"]["url"].endswith("/api/v1/pulumi")
        assert (provider["state"]["project"], provider["state"]["stack"]) == ("proj", "dev")

    async def test_a_stack_that_moved_is_refused(self) -> None:
        """Something else wrote the stack while the run held it."""
        run = _run()
        with _Harness(run, _ws(run), latest=_sv(6), stored=STORED) as h:
            resp = await h.client.put(h.url("?base-serial=4"), content=_export(x=1))
        assert resp.status_code == 409
        h.storage.put.assert_not_awaited()

    async def test_a_retry_of_a_landed_upload_succeeds(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=_sv(5, run_id=run.id), stored=STORED) as h:
            resp = await h.client.put(h.url("?base-serial=4"), content=_export(x=1))
        assert resp.status_code == 200
        h.storage.put.assert_not_awaited()

    async def test_sealed_secrets_are_refused(self) -> None:
        """Sealed under a passphrase that died with the Pod: storing it would
        destroy the secret."""
        run = _run()
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.put(
                h.url("?base-serial=0"), content=_export(pw={**SIG, "ciphertext": "v1:gone"})
            )
        assert resp.status_code == 400
        h.storage.put.assert_not_awaited()

    async def test_a_plan_only_run_writes_nothing(self) -> None:
        run = _run(plan_only=True)
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.put(h.url("?base-serial=0"), content=_export(x=1))
        assert resp.status_code == 409

    async def test_the_base_serial_is_required(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.put(h.url(), content=_export(x=1))
        assert resp.status_code == 400

    async def test_a_body_that_is_not_an_export_is_refused(self) -> None:
        run = _run()
        with _Harness(run, _ws(run), latest=None) as h:
            resp = await h.client.put(h.url("?base-serial=0"), content=b'{"resources": []}')
        assert resp.status_code == 400


def test_the_serial_header_matches_the_runners() -> None:
    """The runner image ships no `api/` package, so the name is written twice."""
    from terrapod.api.routers.run_artifacts import PULUMI_STATE_SERIAL_HEADER as api_side
    from terrapod.runner.phases.state import PULUMI_STATE_SERIAL_HEADER as runner_side

    assert api_side == runner_side
