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

from terrapod.api.dependencies import AuthenticatedUser
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
    from terrapod.api.routers.pulumi_service import pulumi_user

    app = create_application()
    # Override the SURFACE's dependency, not the generic one. Overriding
    # get_current_user here is what made these tests pass while the real CLI got
    # 401 on every request: the endpoints depend on pulumi_user, which
    # translates Pulumi's `token` scheme, and injecting past it skips exactly
    # the code the CLI exercises. The scheme itself is tested below, unmocked.
    app.dependency_overrides[pulumi_user] = lambda: _user()
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
            await _require_lease(request, "u-1", AsyncMock(), "default/p/d")
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
            await _require_lease(request, "u-1", AsyncMock(), "default/p/d")
        assert exc.value.status_code == 401

    async def test_a_wrong_lease_is_refused(self) -> None:
        from terrapod.api.routers.pulumi_service import _require_lease

        redis = AsyncMock()
        redis.hgetall.return_value = {"lease": "the-real-one", "kind": "update"}
        request = MagicMock()
        request.headers = {"authorization": "update-token not-the-real-one"}
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await _require_lease(request, "u-1", AsyncMock(), "default/p/d")
        assert exc.value.status_code == 401

    async def test_the_right_lease_is_accepted(self) -> None:
        from terrapod.api.routers.pulumi_service import _require_lease

        redis = AsyncMock()
        ws = MagicMock(id=uuid.uuid4())
        redis.hgetall.return_value = {
            "lease": "good",
            "kind": "update",
            "workspace_id": str(ws.id),
        }
        request = MagicMock()
        request.headers = {"authorization": "update-token good"}
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch("terrapod.api.routers.pulumi_service._find_stack", AsyncMock(return_value=ws)),
        ):
            record, bound = await _require_lease(request, "u-1", AsyncMock(), "default/p/d")
        assert record["kind"] == "update"
        assert bound is ws

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
                await _require_lease(request, "gone", AsyncMock(), "default/p/d")
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

        redis = AsyncMock()
        redis.set.return_value = None  # SET NX found the key already there
        take = AsyncMock()

        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}.take_workspace_lock", take),
        ):
            with pytest.raises(HTTPException) as exc:
                await _begin_update(_stack_ws(), "update", _user(), AsyncMock())
        assert exc.value.status_code == 409
        # Printed verbatim by the CLI, so it is the whole of the explanation.
        assert exc.value.detail == "another update is currently in progress"
        take.assert_not_awaited()

    async def test_the_first_update_is_allowed_and_locks_the_workspace(self) -> None:
        from terrapod.api.routers.pulumi_service import _begin_update

        ws = _stack_ws()
        redis = AsyncMock()
        redis.set.return_value = True
        take = AsyncMock()

        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}.take_workspace_lock", take),
        ):
            out = await _begin_update(ws, "update", _user(), AsyncMock())
        assert out["updateID"]
        # NX is what makes this a mutex rather than a read-then-write race.
        assert redis.set.await_args.kwargs.get("nx") is True
        # And the workspace lock, as a Terraform CLI apply takes (#1562).
        assert take.await_args.args[1:] == (ws.id, out["updateID"])

    async def test_a_locked_workspace_refuses_the_update_and_says_why(self) -> None:
        from terrapod.api.routers.pulumi_service import _begin_update
        from terrapod.services.pulumi_update_locks import LockRefused

        ws = _stack_ws()
        redis = AsyncMock()
        redis.set.return_value = True
        refused = AsyncMock(side_effect=LockRefused('the workspace is locked (lock ID: "x")'))

        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}.take_workspace_lock", refused),
        ):
            with pytest.raises(HTTPException) as exc:
                await _begin_update(ws, "update", _user(), AsyncMock())
        assert exc.value.status_code == 409
        assert "locked" in exc.value.detail
        # The mutex it had taken is given back, or the next attempt would wait
        # out a lease for an update that never started.
        redis.delete.assert_awaited_once()

    @pytest.mark.parametrize("kind", ["preview"])
    async def test_a_preview_takes_no_lock_at_all(self, kind) -> None:
        """Two previews of one stack used to collide on the update mutex."""
        from terrapod.api.routers.pulumi_service import _begin_update

        redis = AsyncMock()
        take = AsyncMock()
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}.take_workspace_lock", take),
        ):
            out = await _begin_update(_stack_ws(), kind, _user(), AsyncMock())
        assert out["updateID"]
        redis.set.assert_not_awaited()
        take.assert_not_awaited()

    async def test_a_vcs_connected_agent_workspace_refuses_a_local_update(self) -> None:
        """Terraform's rule: such a workspace's changes come from the repository."""
        from terrapod.api.routers.pulumi_service import _begin_update

        ws = _stack_ws(execution_mode="agent", vcs_connection_id=uuid.uuid4())
        redis = AsyncMock()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await _begin_update(ws, "update", _user(), AsyncMock())
        assert exc.value.status_code == 409
        assert "VCS" in exc.value.detail
        redis.set.assert_not_awaited()

    async def test_but_may_still_preview(self) -> None:
        from terrapod.api.routers.pulumi_service import _begin_update

        ws = _stack_ws(execution_mode="agent", vcs_connection_id=uuid.uuid4())
        with patch("terrapod.redis.client.get_redis_client", return_value=AsyncMock()):
            assert (await _begin_update(ws, "preview", _user(), AsyncMock()))["updateID"]


MOD = "terrapod.api.routers.pulumi_service"


def _stack_ws(*, execution_mode: str = "local", vcs_connection_id=None) -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::dev"
    ws.labels = {}
    ws.execution_mode = execution_mode
    ws.vcs_connection_id = vcs_connection_id
    return ws


def _lease_request(body: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {"authorization": "update-token good"}
    request.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    return request


class TestTheLeaseIsRenewed:
    """#1571: the CLI renews part-way through a long update and adopts the token
    in the response. Answering without one made every later call a 401."""

    async def test_the_lease_comes_back(self) -> None:
        from terrapod.api.routers.pulumi_service import renew_lease

        ws = _stack_ws()
        redis = AsyncMock()
        redis.get.return_value = b"u-1"
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(
                f"{MOD}._require_lease",
                AsyncMock(return_value=({"lease": "good", "kind": "update"}, ws)),
            ),
        ):
            out = await renew_lease(
                "default",
                "proj",
                "dev",
                "u-1",
                _lease_request({"token": "", "duration": 300}),
                AsyncMock(),
            )
        assert out == {"token": "good"}

    async def test_both_the_record_and_the_stack_mutex_are_extended(self) -> None:
        from terrapod.api.routers.pulumi_service import LEASE_TTL_SECONDS, renew_lease

        ws = _stack_ws()
        redis = AsyncMock()
        redis.get.return_value = b"u-1"
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._require_lease", AsyncMock(return_value=({"lease": "good"}, ws))),
        ):
            await renew_lease(
                "default", "proj", "dev", "u-1", _lease_request({"duration": 300}), AsyncMock()
            )
        keys = [c.args for c in redis.expire.await_args_list]
        assert ("tp:pulumi:update:u-1", LEASE_TTL_SECONDS) in keys
        assert (f"tp:pulumi:stack_active:{ws.id}", LEASE_TTL_SECONDS) in keys

    async def test_a_longer_duration_than_the_lease_is_honoured(self) -> None:
        from terrapod.api.routers.pulumi_service import LEASE_TTL_SECONDS, renew_lease

        redis = AsyncMock()
        redis.get.return_value = None
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._require_lease", AsyncMock(return_value=({"lease": "g"}, _stack_ws()))),
        ):
            await renew_lease(
                "default",
                "proj",
                "dev",
                "u-1",
                _lease_request({"duration": LEASE_TTL_SECONDS * 2}),
                AsyncMock(),
            )
        assert redis.expire.await_args_list[0].args[1] == LEASE_TTL_SECONDS * 2

    async def test_another_updates_mutex_is_not_extended(self) -> None:
        from terrapod.api.routers.pulumi_service import renew_lease

        redis = AsyncMock()
        redis.get.return_value = b"someone-else"
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._require_lease", AsyncMock(return_value=({"lease": "g"}, _stack_ws()))),
        ):
            await renew_lease("default", "proj", "dev", "u-1", _lease_request(), AsyncMock())
        assert len(redis.expire.await_args_list) == 1


class TestCompletingReleasesTheWorkspace:
    @pytest.mark.parametrize("kind,releases", [("update", True), ("preview", False)])
    async def test_the_workspace_lock_goes_with_the_update(self, kind, releases) -> None:
        from terrapod.api.routers.pulumi_service import complete_update

        ws = _stack_ws()
        redis = AsyncMock()
        redis.get.return_value = b"u-1"
        release = AsyncMock(return_value=True)
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._require_lease", AsyncMock(return_value=({"kind": kind}, ws))),
            patch(f"{MOD}.release_workspace_lock", release),
        ):
            await complete_update(
                "default",
                "proj",
                "dev",
                "u-1",
                _lease_request({"status": "succeeded"}),
                AsyncMock(),
            )
        assert release.called is releases


class TestCancel:
    """`pulumi cancel` reads `activeUpdate`, then posts to the update's cancel
    route with the user's token (#1571)."""

    async def test_the_stack_reports_its_active_update(self) -> None:
        from terrapod.api.routers.pulumi_service import get_stack

        ws = _stack_ws()
        redis = AsyncMock()
        redis.get.return_value = b"u-9"
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._authorized_stack", AsyncMock(return_value=ws)),
        ):
            out = await get_stack("default", "proj", "dev", _user(), AsyncMock())
        assert out["activeUpdate"] == "u-9"

    async def test_an_idle_stack_reports_none(self) -> None:
        from terrapod.api.routers.pulumi_service import get_stack

        redis = AsyncMock()
        redis.get.return_value = None
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._authorized_stack", AsyncMock(return_value=_stack_ws())),
        ):
            out = await get_stack("default", "proj", "dev", _user(), AsyncMock())
        assert "activeUpdate" not in out

    async def test_cancelling_ends_the_update_and_frees_the_stack(self) -> None:
        from terrapod.api.routers.pulumi_service import cancel_update

        ws = _stack_ws()
        redis = AsyncMock()
        redis.hgetall.return_value = {"kind": "update", "workspace_id": str(ws.id), "lease": "l"}
        redis.get.return_value = b"u-1"
        release = AsyncMock(return_value=True)
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._authorized_stack", AsyncMock(return_value=ws)),
            patch(f"{MOD}.release_workspace_lock", release),
        ):
            assert await cancel_update("default", "proj", "dev", "u-1", _user(), AsyncMock()) == {}
        deleted = [c.args[0] for c in redis.delete.await_args_list]
        # The record goes, so the running CLI's lease stops working...
        assert "tp:pulumi:update:u-1" in deleted
        # ...and the stack is free for the next update.
        assert f"tp:pulumi:stack_active:{ws.id}" in deleted
        release.assert_awaited_once()

    async def test_an_unknown_update_is_a_404(self) -> None:
        from terrapod.api.routers.pulumi_service import cancel_update

        redis = AsyncMock()
        redis.hgetall.return_value = {}
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            with pytest.raises(HTTPException) as exc:
                await cancel_update("default", "proj", "dev", "gone", _user(), AsyncMock())
        assert exc.value.status_code == 404

    async def test_cancelling_costs_what_beginning_cost(self) -> None:
        """A destroy is cancelled with the destroy capability, read from the
        update, not with whatever the URL implies."""
        from terrapod.api.routers.pulumi_service import cancel_update
        from terrapod.auth import capabilities as cap

        ws = _stack_ws()
        redis = AsyncMock()
        redis.hgetall.return_value = {"kind": "destroy", "workspace_id": str(ws.id)}
        redis.get.return_value = None
        authorized = AsyncMock(return_value=ws)
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._authorized_stack", authorized),
            patch(f"{MOD}.release_workspace_lock", AsyncMock()),
        ):
            await cancel_update("default", "proj", "dev", "u-1", _user(), AsyncMock())
        assert authorized.await_args.args[3] == cap.RUN_APPLY_DESTROY

    async def test_an_update_on_another_stack_cannot_be_cancelled_through_this_one(
        self,
    ) -> None:
        from terrapod.api.routers.pulumi_service import cancel_update

        redis = AsyncMock()
        redis.hgetall.return_value = {"kind": "update", "workspace_id": str(uuid.uuid4())}
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(f"{MOD}._authorized_stack", AsyncMock(return_value=_stack_ws())),
        ):
            with pytest.raises(HTTPException) as exc:
                await cancel_update("default", "proj", "dev", "u-1", _user(), AsyncMock())
        assert exc.value.status_code == 404
        redis.delete.assert_not_awaited()


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
                "terrapod.api.routers.pulumi_service._authorized_stack",
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


class TestTheAuthSchemeTheCliActuallySends:
    """`Authorization: token <api-token>` — NOT Bearer.

    This is the gap a live run found after 21 tests passed: the scheme was
    documented in the module docstring and then not implemented, because every
    test injected its own authenticated client and never sent the header. So
    these exercise the dependency directly, with no override in the way.
    """

    async def test_the_token_scheme_is_accepted(self) -> None:
        from terrapod.api.routers.pulumi_service import pulumi_user

        request = MagicMock()
        request.headers = {"authorization": "token an-api-token"}
        resolved = AsyncMock(return_value=_user())
        with patch("terrapod.api.dependencies.get_current_user", resolved):
            out = await pulumi_user(request, AsyncMock())
        assert out.email == "a@b.c"
        # The value is handed on as a normal credential, so one place keeps
        # resolving API tokens, sessions and roles.
        assert resolved.await_args.args[1].credentials == "an-api-token"

    async def test_bearer_is_also_accepted(self) -> None:
        """An operator reaching for curl while debugging is reasonable, and
        refusing it buys nothing."""
        from terrapod.api.routers.pulumi_service import pulumi_user

        request = MagicMock()
        request.headers = {"authorization": "Bearer an-api-token"}
        with patch("terrapod.api.dependencies.get_current_user", AsyncMock(return_value=_user())):
            assert (await pulumi_user(request, AsyncMock())).email == "a@b.c"

    async def test_no_credential_is_a_401_naming_the_scheme(self) -> None:
        """The error has to say what the CLI should send; "unauthorized" alone
        leaves an operator guessing at a scheme most tools do not use."""
        from terrapod.api.routers.pulumi_service import pulumi_user

        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc:
            await pulumi_user(request, AsyncMock())
        assert exc.value.status_code == 401
        assert "token" in exc.value.detail


class TestErrorsAreShapedForTheCli:
    """The CLI reads `message` and prints it verbatim.

    A live `pulumi stack init` against the house envelope produced
    `error: could not create stack: [0] ` — a failure with no explanation. It
    matters most on the 409, where refuse-to-start IS the concurrency model and
    the message is all the operator gets.
    """

    async def test_an_error_carries_a_message_field(self) -> None:
        # `execute()` is awaited and its RESULT is sync, so the result must be a
        # MagicMock — a bare AsyncMock hands back a coroutine and the lookup
        # never reaches its "not found" branch.
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result

        async with await _client(_app(db)) as c:
            r = await c.get(f"{BASE}/stacks/default/nope/nope")
        assert r.status_code == 404
        body = r.json()
        assert body["message"], f"the CLI would print an empty error: {body}"
        assert body["code"] == 404

    async def test_other_surfaces_keep_the_house_envelope(self) -> None:
        """The branch is scoped to this surface; changing the envelope everywhere
        would be a silent, repo-wide break."""
        async with await _client(_app()) as c:
            r = await c.get("/api/v1/workspaces/ws-does-not-exist")
        assert "errors" in r.json() or "detail" in r.json()
