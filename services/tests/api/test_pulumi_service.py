"""The Pulumi service surface (#1522).

What is worth testing here is set by what the #1502 capture found, because those
are the things a synthetic client gets wrong: the second auth scheme that only
appears mid-update, the lease the CLI aborts without, `deployment: null` for an
empty stack, gzipped bodies, and refuse-to-start concurrency.

Each of those failed a real `pulumi` run during the capture before it was
understood, so each gets a test that would fail the same way.
"""

from __future__ import annotations

import base64
import gzip
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/pulumi/api"


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="a@b.c",
        display_name="A",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app(db=None):
    from terrapod.api.app import create_application

    app = create_application()
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: db or AsyncMock()
    return app


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestTheSurfaceIsMountedWhereTheCliWillLookForIt:
    async def test_login_answers_under_the_native_prefix(self) -> None:
        """The CLI appends `/api/...` to the base URL it was given, prefix
        included — which is what lets this be mounted natively instead of taking
        root space. `pulumi login` stops dead if /api/user does not answer.
        """
        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/user")
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "a@b.c"
        assert [o["name"] for o in body["organizations"]] == ["default"]

    async def test_it_does_not_take_root_space(self) -> None:
        """Root is reserved for the surfaces genuinely forced there — the OCI
        registry and the terraform discovery document. Pulumi is not one."""
        from terrapod.api.app import app

        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/user" not in paths
        assert any("/pulumi/api/user" in p for p in paths)

    async def test_capabilities_negotiates_nothing_extra(self) -> None:
        """Declaring a capability Terrapod does not implement is how the CLI is
        led into calling an endpoint that is not there."""
        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/capabilities")
        assert r.status_code == 200
        assert r.json() == {"capabilities": []}


class TestTheSecondAuthScheme:
    """The finding invisible to any test that injects an authenticated client.

    Calls addressed at a stack use the user's API token; the three made *during*
    an update use `Authorization: update-token <lease>`. Both halves matter — the
    lease must be accepted, and an absent or wrong one must be refused.
    """

    async def test_an_in_update_call_is_refused_without_a_lease(self) -> None:
        from terrapod.api.routers.pulumi_service import _require_lease

        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc:
            await _require_lease(request, "u-1")
        assert exc.value.status_code == 401
        assert "update-token" in exc.value.detail

    async def test_a_bearer_token_is_not_accepted_in_place_of_a_lease(self) -> None:
        """The scheme is `update-token`, not `Bearer` and not `token`. Accepting
        the wrong one would make the endpoint reachable with a credential the CLI
        never sends there."""
        from terrapod.api.routers.pulumi_service import _require_lease

        request = MagicMock()
        request.headers = {"authorization": "Bearer some-api-token"}
        with pytest.raises(HTTPException) as exc:
            await _require_lease(request, "u-1")
        assert exc.value.status_code == 401

    async def test_a_wrong_lease_is_refused(self) -> None:
        from terrapod.api.routers.pulumi_service import _require_lease

        redis = AsyncMock()
        redis.hgetall.return_value = {"lease": "the-real-one", "kind": "update"}
        request = MagicMock()
        request.headers = {"authorization": "update-token not-the-real-one"}
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await _require_lease(request, "u-1")
        assert exc.value.status_code == 401

    async def test_the_right_lease_is_accepted(self) -> None:
        from terrapod.api.routers.pulumi_service import _require_lease

        redis = AsyncMock()
        redis.hgetall.return_value = {"lease": "good", "kind": "update"}
        request = MagicMock()
        request.headers = {"authorization": "update-token good"}
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            record = await _require_lease(request, "u-1")
        assert record["kind"] == "update"

    async def test_an_expired_lease_reads_as_invalid(self) -> None:
        """A lease is a TTL, so "expired" and "never existed" are the same
        answer — which is what lets a dead run release its stack."""
        from terrapod.api.routers.pulumi_service import _require_lease

        redis = AsyncMock()
        redis.hgetall.return_value = {}
        request = MagicMock()
        request.headers = {"authorization": "update-token whatever"}
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await _require_lease(request, "gone")
        assert exc.value.status_code == 401


class TestGzippedBodies:
    """Checkpoint and event bodies arrive gzipped. A handler that parses the raw
    body sees binary and fails on what looks like malformed JSON."""

    async def test_a_gzipped_body_is_decompressed(self) -> None:
        from terrapod.api.routers.pulumi_service import read_body

        payload = {"deployment": {"resources": []}}
        request = MagicMock()
        request.headers = {"content-encoding": "gzip"}
        request.body = AsyncMock(return_value=gzip.compress(json.dumps(payload).encode()))
        assert await read_body(request) == payload

    async def test_a_plain_body_still_parses(self) -> None:
        from terrapod.api.routers.pulumi_service import read_body

        request = MagicMock()
        request.headers = {}
        request.body = AsyncMock(return_value=b'{"a": 1}')
        assert await read_body(request) == {"a": 1}

    async def test_a_body_that_lies_about_being_gzipped_is_a_400(self) -> None:
        """Rather than a 500 from deep in the JSON parser, which tells the
        operator nothing about which side was wrong."""
        from terrapod.api.routers.pulumi_service import read_body

        request = MagicMock()
        request.headers = {"content-encoding": "gzip"}
        request.body = AsyncMock(return_value=b"not actually gzip")
        with pytest.raises(HTTPException) as exc:
            await read_body(request)
        assert exc.value.status_code == 400
        assert "gzip" in exc.value.detail


class TestConcurrency:
    """Refuse-to-start. A 409 on begin ends the CLI immediately, printing the
    service's message verbatim — so the message is the whole explanation."""

    async def test_a_second_update_is_refused_while_one_is_in_flight(self) -> None:
        from terrapod.api.routers.pulumi_service import _begin_update

        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.name = "proj::dev"
        redis = AsyncMock()
        redis.set.return_value = None  # SET NX found the key already there

        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await _begin_update(ws, "update", _user())
        assert exc.value.status_code == 409
        # Printed verbatim by the CLI, so it is the whole of the explanation.
        assert exc.value.detail == "another update is currently in progress"

    async def test_the_first_update_is_allowed(self) -> None:
        from terrapod.api.routers.pulumi_service import _begin_update

        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.name = "proj::dev"
        redis = AsyncMock()
        redis.set.return_value = True

        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            out = await _begin_update(ws, "update", _user())
        assert out["updateID"]
        # NX is what makes this a mutex rather than a read-then-write race.
        assert redis.set.await_args.kwargs.get("nx") is True


class TestStackIdentity:
    def test_a_stack_maps_onto_one_workspace_name(self) -> None:
        from terrapod.api.routers.pulumi_service import _stack_workspace_name

        assert _stack_workspace_name("proj", "dev") == "proj::dev"

    def test_a_malformed_stack_id_is_a_400_not_a_404(self) -> None:
        """404 means "does not exist, create it" to the CLI. Answering that to a
        request this API cannot address at all would invite it to try."""
        from terrapod.api.routers.pulumi_service import _split_stack_id

        with pytest.raises(HTTPException) as exc:
            _split_stack_id("only-one-part")
        assert exc.value.status_code == 400

    def test_a_well_formed_id_splits(self) -> None:
        from terrapod.api.routers.pulumi_service import _split_stack_id

        assert _split_stack_id("default/proj/dev") == ("default", "proj", "dev")


class TestSecrets:
    """The service is the stack's secrets provider — `encrypt` is called during
    an ordinary `up`, so this is an obligation rather than a convenience."""

    async def test_a_value_round_trips(self) -> None:
        from terrapod.api.routers.pulumi_service import decrypt_secret, encrypt_secret

        svc = MagicMock()
        svc.encrypt.side_effect = lambda p: f"sealed:{p}"
        svc.decrypt.side_effect = lambda s: s.removeprefix("sealed:")

        secret = "hunter2"
        enc_req = MagicMock()
        enc_req.headers = {}
        enc_req.body = AsyncMock(
            return_value=json.dumps(
                {"plaintext": base64.b64encode(secret.encode()).decode()}
            ).encode()
        )

        with (
            patch("terrapod.crypto.service.get_encryption", return_value=svc),
            patch(
                "terrapod.api.routers.pulumi_service._load_stack",
                AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ),
        ):
            out = await encrypt_secret("default", "p", "d", enc_req, _user(), AsyncMock())

            dec_req = MagicMock()
            dec_req.headers = {}
            dec_req.body = AsyncMock(
                return_value=json.dumps({"ciphertext": out["ciphertext"]}).encode()
            )
            back = await decrypt_secret("default", "p", "d", dec_req, _user(), AsyncMock())

        assert base64.b64decode(back["plaintext"]).decode() == secret


class TestTheEngineGate:
    """#1429: off means ABSENT, not present-and-404ing.

    A surface that refuses every request is still in the schema, still carries
    its dependencies, and still reads to an auditor as something this deployment
    does. The registry shipped with an `enabled` flag nothing read, which is the
    worked example this guards against.
    """

    def _paths(self, *, pulumi: bool) -> set[str]:
        from terrapod.api.app import create_application

        with patch(
            "terrapod.services.engine_gating.engine_enabled",
            side_effect=lambda e: pulumi if e == "pulumi" else True,
        ):
            app = create_application()
        return {getattr(r, "path", "") for r in app.routes}

    def test_the_surface_is_absent_when_the_engine_is_off(self) -> None:
        assert not any("/pulumi/api/" in p for p in self._paths(pulumi=False))

    def test_the_surface_is_present_when_the_engine_is_on(self) -> None:
        paths = self._paths(pulumi=True)
        assert any("/pulumi/api/user" in p for p in paths)
        assert any("/pulumi/api/stacks/" in p for p in paths)

    def test_terraform_surfaces_are_untouched_either_way(self) -> None:
        """The reason the gate exists: the great majority of users came for
        terraform and openTofu, and multi-engine ambition must cost them
        nothing. The provider mirror, binary cache and module registry are never
        gateable."""
        for pulumi in (True, False):
            paths = self._paths(pulumi=pulumi)
            assert any(p.startswith("/api/tfe/v2/") for p in paths), f"pulumi={pulumi}"
            assert any("/provider-mirror/" in p for p in paths), f"pulumi={pulumi}"
            assert any("/binary-cache/" in p for p in paths), f"pulumi={pulumi}"
            assert any("/registry-modules" in p for p in paths), f"pulumi={pulumi}"

    def test_gating_it_off_deletes_nothing(self) -> None:
        """Turning an engine off hides and halts; stored content survives and
        returns on re-enable. Asserted structurally: nothing in the router
        performs a delete outside the explicit `stack rm` endpoint."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/pulumi_service.py"
        ).read_text()
        # db.delete appears once, in delete_stack — an explicit user action, not
        # a consequence of gating.
        assert src.count("db.delete(") == 1
