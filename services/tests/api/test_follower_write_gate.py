"""The follower write gate (#1130).

Two properties, and they pull in opposite directions:

- A follower must refuse writes it cannot carry anywhere, because settings
  replication is leader → follower and a write made here is reverted at the next
  reconciliation.
- A follower must stay **usable** — an operator has to log in and read its HA
  status before deciding to move DNS. A gate that locked them out would be a
  worse failure than the one it prevents, because it would strike during the
  incident it exists to help with.

The third property is the one that matters to every install that is *not* a
pair: on a single node the gate is inert. That is asserted directly rather than
assumed, since a false positive here takes writes down on a healthy node.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.follower_gate import (
    FOLLOWER_WRITABLE_PATHS,
    FOLLOWER_WRITABLE_PREFIXES,
    WRITE_METHODS,
    follower_write_gate,
    is_follower_writable,
)

MANAGEMENT_WRITES = [
    ("POST", "/api/terrapod/v1/workspaces"),
    ("PATCH", "/api/terrapod/v1/workspaces/ws-1"),
    ("DELETE", "/api/terrapod/v1/workspaces/ws-1"),
    ("POST", "/api/terrapod/v1/roles"),
    ("POST", "/api/terrapod/v1/agent-pools"),
    ("PATCH", "/api/terrapod/v1/vcs-connections/vcs-1"),
    ("POST", "/api/terrapod/v1/catalog-items"),
    ("PUT", "/api/v2/state-versions/sv-1/content"),
    ("POST", "/oauth/token"),
]


def _app() -> FastAPI:
    """An app carrying only the gate, so what is asserted is the gate."""
    app = FastAPI()
    app.middleware("http")(follower_write_gate)

    async def ok() -> dict[str, bool]:
        return {"reached": True}

    seen = set()
    for method, path in MANAGEMENT_WRITES:
        key = (path, method)
        if key in seen:
            continue
        seen.add(key)
        app.add_api_route(path, ok, methods=[method])
    for path in sorted(FOLLOWER_WRITABLE_PATHS):
        app.add_api_route(path, ok, methods=["POST"])
    app.add_api_route(FOLLOWER_WRITABLE_PREFIXES[0] + "{email}", ok, methods=["DELETE", "POST"])
    app.add_api_route("/api/terrapod/v1/workspaces", ok, methods=["GET"])
    return app


async def _request(method: str, path: str, *, leader: bool):
    with patch("terrapod.api.follower_gate.is_leader", new=AsyncMock(return_value=leader)):
        transport = ASGITransport(app=_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path)


class TestAFollowerRefusesWrites:
    @pytest.mark.parametrize(("method", "path"), MANAGEMENT_WRITES)
    async def test_management_writes_are_refused(self, method: str, path: str):
        resp = await _request(method, path, leader=False)

        assert resp.status_code == 503, f"{method} {path} was not refused on a follower"

    async def test_the_refusal_says_where_to_retry(self):
        """A 503 with no explanation sends the operator to the logs. The point
        of the message is that the next step is on the other node."""
        resp = await _request("POST", "/api/terrapod/v1/workspaces", leader=False)

        assert "not the leader" in resp.json()["detail"]
        assert "shared name" in resp.json()["detail"]

    async def test_the_refusal_carries_the_jsonapi_envelope(self):
        """House style (#1063): both keys, so neither client shape breaks."""
        body = (await _request("POST", "/api/terrapod/v1/workspaces", leader=False)).json()

        assert body["errors"][0]["status"] == "503"
        assert "detail" in body

    async def test_reads_are_untouched(self):
        """The whole point of a warm standby is that you can look at it."""
        resp = await _request("GET", "/api/terrapod/v1/workspaces", leader=False)

        assert resp.status_code == 200

    async def test_the_token_mint_is_refused(self):
        """`api_tokens` is replicated, so a token minted here is erased at the
        next reconciliation. Handing out a credential that later vanishes is
        worse than refusing it."""
        resp = await _request("POST", "/oauth/token", leader=False)

        assert resp.status_code == 503


class TestAFollowerStaysUsable:
    """The failure this guards against strikes during an incident: an operator
    who cannot log in to the standby cannot check it is caught up."""

    @pytest.mark.parametrize("path", sorted(FOLLOWER_WRITABLE_PATHS))
    async def test_login_and_logout_still_work(self, path: str):
        resp = await _request("POST", path, leader=False)

        assert resp.status_code == 200, f"{path} must remain usable on a follower"

    async def test_session_revocation_still_works(self):
        """It only ever removes access, and only on this node."""
        resp = await _request(
            "DELETE",
            "/api/terrapod/v1/auth/sessions/user/someone@example.com",
            leader=False,
        )

        assert resp.status_code == 200


class TestALeaderIsUnaffected:
    """The dangerous direction. A single node runs `role: leader`, so every one
    of these paths is the normal path for almost every install."""

    @pytest.mark.parametrize(("method", "path"), MANAGEMENT_WRITES)
    async def test_every_write_passes_on_a_leader(self, method: str, path: str):
        resp = await _request(method, path, leader=True)

        assert resp.status_code == 200, f"{method} {path} was refused on a leader"

    async def test_the_shipped_default_never_consults_redis(self):
        """`role: leader` is answered from configuration. If this started
        touching Redis, every single-node install would gain a dependency it
        never asked for on its write path."""
        from terrapod.config import settings

        assert settings.ha.role == "leader", "the shipped default must stay leader"

        with patch("terrapod.services.ha_role.get_redis_client") as redis:
            resp = await _request("POST", "/api/terrapod/v1/workspaces", leader=True)

        assert resp.status_code == 200
        redis.assert_not_called()


class TestTheAllowListIsPinned:
    """Default-deny only holds while the exception list stays small and
    deliberate. Pinning it makes growing it a reviewed act rather than a drift.
    """

    def test_the_allow_list_is_exactly_this(self):
        assert FOLLOWER_WRITABLE_PATHS == {
            "/api/terrapod/v1/auth/local/authorize",
            "/api/terrapod/v1/auth/local/login",
            "/api/terrapod/v1/auth/saml/acs",
            "/api/terrapod/v1/auth/token",
            "/api/terrapod/v1/auth/logout",
            "/api/terrapod/v1/auth/logout/all",
        }, (
            "The follower allow-list changed. Every entry must be something that "
            "RECORDS or REDUCES access on this node — never something that changes "
            "platform state, because a follower cannot carry that anywhere."
        )

    def test_the_prefix_list_is_exactly_this(self):
        assert FOLLOWER_WRITABLE_PREFIXES == ("/api/terrapod/v1/auth/sessions/user/",)

    def test_every_allowed_path_is_under_auth(self):
        """A structural restatement of the principle: nothing outside the auth
        surface has any business being writable on a follower."""
        for path in FOLLOWER_WRITABLE_PATHS:
            assert path.startswith("/api/terrapod/v1/auth/"), path
        for prefix in FOLLOWER_WRITABLE_PREFIXES:
            assert prefix.startswith("/api/terrapod/v1/auth/"), prefix

    def test_the_gate_covers_every_mutating_method(self):
        assert WRITE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}

    def test_an_unlisted_auth_path_is_not_allowed_by_accident(self):
        """The prefix rule must not widen into the whole auth surface."""
        assert not is_follower_writable("/api/terrapod/v1/auth/sessions")
        assert not is_follower_writable("/api/terrapod/v1/auth/local/register")


class TestTheGateIsWiredIn:
    """A gate that is written but not registered is worse than none, because
    the tests above would still pass."""

    def test_the_real_app_carries_it(self):
        from terrapod.api.app import app

        names = [
            getattr(m.kwargs.get("dispatch", None), "__name__", "") for m in app.user_middleware
        ]

        assert "follower_write_gate" in names

    def test_it_is_the_innermost_user_middleware(self):
        """Innermost, so a refusal still passes back out through the audit log,
        the security headers and the metrics counter. Registered outermost it
        would be invisible to all three."""
        from terrapod.api.app import app

        names = [
            getattr(m.kwargs.get("dispatch", None), "__name__", "") for m in app.user_middleware
        ]

        assert names[-1] == "follower_write_gate", (
            f"the gate must be the innermost user middleware, stack is {names}"
        )
