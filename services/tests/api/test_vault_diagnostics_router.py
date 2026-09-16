"""Vault diagnostics endpoint tests (#1663): RBAC, shapes, the rate limit.

The service behaviour (what a probe or a check finds) is tested in
``tests/services/test_vault_diagnostics.py``. These pin the HTTP contract: who
may call each endpoint, what shape comes back, that the status endpoint never
contacts Vault, and that the reference check is rate-limited per user.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.errors import jsonapi_error_response
from terrapod.api.routers.vault_diagnostics import router
from terrapod.auth.capabilities import caps_for_level
from terrapod.config import Settings, VaultConfig, VaultInstanceConfig
from terrapod.db.session import get_db
from terrapod.services import vault_client, vault_diagnostics

SECRET = "ROUTER-SECRET-MUST-NOT-LEAK"
WS_ID = uuid.uuid4()
VS_ID = uuid.uuid4()
REF = {"mount": "secret", "path": "apps/x", "field": "token"}


def _user(roles=None, email="dev@example.com"):
    return AuthenticatedUser(
        email=email,
        display_name="Dev",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _settings(enabled=True) -> Settings:
    s = Settings()
    s.vault = VaultConfig(
        enabled=enabled,
        instances=[
            VaultInstanceConfig(
                name="default",
                address="https://vault.test:8200",
                auth={"method": "token", "mount": "token", "role": "terrapod"},
            )
        ]
        if enabled
        else [],
    )
    return s


def _db(*results):
    """A mock session whose execute() yields each result's scalar in turn."""
    db = AsyncMock()
    # add_all is synchronous on a real AsyncSession; as an AsyncMock it returns
    # a coroutine nobody awaits, which only surfaces as a RuntimeWarning.
    db.add_all = MagicMock()
    rows = []
    for r in results:
        m = MagicMock()
        m.scalar_one_or_none.return_value = r
        rows.append(m)
    db.execute.side_effect = rows
    return db


def _ws(mode="agent"):
    ws = MagicMock()
    ws.id = WS_ID
    ws.execution_mode = mode
    return ws


def _app(user, db=None) -> FastAPI:
    app = FastAPI()

    # The app factory's dual-key error envelope, so status and headers are
    # asserted as a real client sees them.
    @app.exception_handler(StarletteHTTPException)
    async def _errors(_request, exc):
        return jsonapi_error_response(
            exc.detail, exc.status_code, headers=getattr(exc, "headers", None)
        )

    app.include_router(router, prefix="/api/terrapod/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db or AsyncMock()
    return app


async def _call(app, method, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, **kw)


def _body(attrs):
    return {"data": {"type": "vault-reference-checks", "attributes": attrs}}


class FakeRedis:
    def __init__(self):
        self.strings: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    async def get(self, key):
        return self.strings.get(key)

    async def set(self, key, value, ex=None):
        self.strings[key] = value

    def pipeline(self, transaction=False):
        redis = self

        class _P:
            def __init__(self):
                self.keys = []

            def incr(self, key):
                self.keys.append(key)

            def expire(self, key, seconds):
                pass

            async def execute(self):
                (key,) = self.keys
                redis.counters[key] = redis.counters.get(key, 0) + 1
                return [redis.counters[key], True]

        return _P()


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        yield fake


@pytest.fixture
def vault_on():
    with patch.object(vault_diagnostics, "settings", _settings()):
        yield


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setenv("TERRAPOD_VAULT_DEFAULT_SECRET", "s.static")
    vault_client.reset_token_cache()
    yield
    vault_client.reset_token_cache()


def _fake_vault(kv=None, caps=("read",)):
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        p = request.url.path
        if p == "/v1/sys/capabilities-self":
            return httpx.Response(200, json={"capabilities": list(caps)})
        return httpx.Response(200, json={"data": {"data": kv or {"token": SECRET}}})

    real = httpx.AsyncClient

    def factory(*_a, **_kw):
        return real(transport=httpx.MockTransport(handler))

    return seen, patch.object(vault_client.httpx, "AsyncClient", factory)


# ── GET /admin/vault ─────────────────────────────────────────────────────


class TestStatusEndpoint:
    @pytest.mark.parametrize("role", ["admin", "audit"])
    async def test_admin_and_audit_may_read(self, role, redis, vault_on):
        resp = await _call(_app(_user([role])), "GET", "/api/terrapod/v1/admin/vault")
        assert resp.status_code == 200
        (item,) = resp.json()["data"]
        assert item["type"] == "vault-instance-statuses"
        assert item["id"] == "default"
        assert item["attributes"]["auth-method"] == "token"

    async def test_everyone_else_is_refused(self, redis, vault_on):
        resp = await _call(_app(_user(["everyone"])), "GET", "/api/terrapod/v1/admin/vault")
        assert resp.status_code == 403
        assert resp.json()["errors"][0]["status"] == "403"

    async def test_disabled_is_an_empty_list(self, redis):
        with patch.object(vault_diagnostics, "settings", _settings(enabled=False)):
            resp = await _call(_app(_user(["admin"])), "GET", "/api/terrapod/v1/admin/vault")
        body = resp.json()
        assert body["data"] == []
        assert body["meta"]["vault"]["enabled"] is False
        assert body["meta"]["pagination"]["total-count"] == 0

    async def test_the_endpoint_reads_redis_only(self, redis, vault_on):
        """Opening the page must never log in to Vault or probe it."""
        redis.strings[vault_diagnostics.STATUS_KEY] = json.dumps(
            {
                "sampled-at": "2026-09-15T10:00:00Z",
                "instances": [
                    {
                        "name": "default",
                        "reachable": True,
                        "sealed": False,
                        "login-ok": True,
                        "ttl-seconds": 1200,
                        "checked-at": "2026-09-15T10:00:00Z",
                    }
                ],
            }
        )

        def boom(*_a, **_kw):
            raise AssertionError("the status endpoint must not open an HTTP client")

        with (
            patch.object(vault_client.httpx, "AsyncClient", boom),
            patch.object(vault_client, "_login", AsyncMock(side_effect=AssertionError)),
            patch.object(
                vault_diagnostics, "probe_instance", AsyncMock(side_effect=AssertionError)
            ),
        ):
            resp = await _call(_app(_user(["admin"])), "GET", "/api/terrapod/v1/admin/vault")
        assert resp.status_code == 200
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["reachable"] is True and attrs["ttl-seconds"] == 1200
        assert resp.json()["meta"]["vault"]["sampled-at"] == "2026-09-15T10:00:00Z"


# ── POST /workspaces/{id}/vault-reference-checks ─────────────────────────


def _patch_caps(level):
    return patch(
        "terrapod.api.routers.vault_diagnostics.resolve_workspace_capabilities_for",
        AsyncMock(return_value=caps_for_level(level)),
    )


WS_PATH = f"/api/terrapod/v1/workspaces/ws-{WS_ID}/vault-reference-checks"


class TestTheCheckAuditsItsReads:
    """#1651 records every Vault read a run makes as a `vault.read` row. The
    reference check reads a secret too — to list its key names — and recorded
    nothing (#1688). The middleware's own row names the endpoint, not the
    instance, mount and path, so an operator reconciling Terrapod's audit log
    against the server's own could not attribute those reads.
    """

    async def test_a_key_listing_writes_a_vault_read_row(self, redis, vault_on):
        db = _db(_ws())
        _seen, patched = _fake_vault()
        with _patch_caps("write"), patched:
            resp = await _call(_app(_user(), db), "POST", WS_PATH, json=_body({"reference": REF}))

        assert resp.status_code == 200, resp.text
        (rows,) = db.add_all.call_args.args
        (row,) = rows
        assert row.action == "vault.read"
        assert row.resource_type == "workspaces" and row.resource_id == f"ws-{WS_ID}"
        assert row.actor_email == "dev@example.com"
        detail = json.loads(row.detail)
        assert detail["instance"] == "default"
        assert detail["mount"] == "secret" and detail["path"] == "apps/x"
        assert detail["phase"] == "check" and detail["outcome"] == "ok"
        # Names and coordinates only, exactly as a run's read is recorded.
        assert SECRET not in row.detail
        db.commit.assert_awaited()

    async def test_a_check_that_reads_no_secret_writes_no_row(self, redis, vault_on):
        """Without `run:plan` the key listing is skipped, so there is no read to
        record: the row follows the read, not the request."""
        db = _db(_ws())
        caps = caps_for_level("write") - {"run:plan"}
        _seen, patched = _fake_vault()
        with (
            patch(
                "terrapod.api.routers.vault_diagnostics.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps),
            ),
            patched,
        ):
            resp = await _call(_app(_user(), db), "POST", WS_PATH, json=_body({"reference": REF}))

        assert resp.status_code == 200, resp.text
        db.add_all.assert_not_called()


class TestWorkspaceCheckRbac:
    async def test_an_unknown_workspace_is_404(self, redis, vault_on):
        resp = await _call(
            _app(_user(), _db(None)), "POST", WS_PATH, json=_body({"reference": REF})
        )
        assert resp.status_code == 404

    async def test_a_malformed_workspace_id_is_404(self, redis, vault_on):
        path = "/api/terrapod/v1/workspaces/ws-not-a-uuid/vault-reference-checks"
        resp = await _call(_app(_user(), _db()), "POST", path, json=_body({"reference": REF}))
        assert resp.status_code == 404

    @pytest.mark.parametrize("level", ["read", "plan"])
    async def test_without_variable_write_it_is_refused(self, level, redis, vault_on):
        check = AsyncMock()
        with _patch_caps(level), patch.object(vault_diagnostics, "check_reference", check):
            resp = await _call(
                _app(_user(), _db(_ws())), "POST", WS_PATH, json=_body({"reference": REF})
            )
        assert resp.status_code == 403
        check.assert_not_called()

    async def test_with_write_it_checks_and_may_list_keys(self, redis, vault_on):
        seen, patched = _fake_vault()
        with _patch_caps("write"), patched:
            resp = await _call(
                _app(_user(), _db(_ws())), "POST", WS_PATH, json=_body({"reference": REF})
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "vault-reference-checks" and data["id"].startswith("vrc-")
        attrs = data["attributes"]
        assert attrs["ok"] is True and attrs["keys"] == ["token"]
        assert SECRET not in resp.text
        assert "/v1/secret/data/apps/x" in seen

    async def test_without_plan_permission_keys_are_withheld(self, redis, vault_on):
        caps = caps_for_level("write") - {"run:plan"}
        seen, patched = _fake_vault()
        with (
            patch(
                "terrapod.api.routers.vault_diagnostics.resolve_workspace_capabilities_for",
                AsyncMock(return_value=caps),
            ),
            patched,
        ):
            resp = await _call(
                _app(_user(), _db(_ws())), "POST", WS_PATH, json=_body({"reference": REF})
            )
        attrs = resp.json()["data"]["attributes"]
        assert attrs["keys"] is None
        assert "keys-need-plan-permission" in attrs["notes"]
        assert "/v1/secret/data/apps/x" not in seen

    async def test_a_local_workspace_is_noted(self, redis, vault_on):
        _seen, patched = _fake_vault()
        with _patch_caps("write"), patched:
            resp = await _call(
                _app(_user(), _db(_ws("local"))), "POST", WS_PATH, json=_body({"reference": REF})
            )
        assert "local-execution" in resp.json()["data"]["attributes"]["notes"]

    async def test_a_body_without_a_reference_is_422(self, redis, vault_on):
        with _patch_caps("write"):
            resp = await _call(_app(_user(), _db(_ws())), "POST", WS_PATH, json=_body({}))
        assert resp.status_code == 422

    async def test_a_reference_that_does_not_parse_is_a_result_not_an_error(self, redis, vault_on):
        with _patch_caps("write"):
            resp = await _call(
                _app(_user(), _db(_ws())),
                "POST",
                WS_PATH,
                json=_body({"reference": {"mount": "secret"}}),
            )
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["parses"] is False and "missing" in attrs["parse-error"]


class TestWorkspaceCheckByVariable:
    async def test_a_stored_vault_variable_is_checked(self, redis, vault_on):
        var = MagicMock(key="TOKEN", value=json.dumps(REF), value_source="vault")
        _seen, patched = _fake_vault()
        with (
            _patch_caps("write"),
            patched,
            patch(
                "terrapod.api.routers.vault_diagnostics.variable_service.get_variable",
                AsyncMock(return_value=var),
            ),
        ):
            resp = await _call(
                _app(_user(), _db(_ws())),
                "POST",
                WS_PATH,
                json=_body({"variable-id": f"var-{uuid.uuid4()}"}),
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["ok"] is True

    async def test_a_static_variable_is_422(self, redis, vault_on):
        var = MagicMock(key="X", value="literal", value_source="static")
        with (
            _patch_caps("write"),
            patch(
                "terrapod.api.routers.vault_diagnostics.variable_service.get_variable",
                AsyncMock(return_value=var),
            ),
        ):
            resp = await _call(
                _app(_user(), _db(_ws())),
                "POST",
                WS_PATH,
                json=_body({"variable-id": f"var-{uuid.uuid4()}"}),
            )
        assert resp.status_code == 422
        assert "literal" not in resp.text

    async def test_an_unknown_variable_is_404(self, redis, vault_on):
        with (
            _patch_caps("write"),
            patch(
                "terrapod.api.routers.vault_diagnostics.variable_service.get_variable",
                AsyncMock(return_value=None),
            ),
        ):
            resp = await _call(
                _app(_user(), _db(_ws())),
                "POST",
                WS_PATH,
                json=_body({"variable-id": "var-nope"}),
            )
        assert resp.status_code == 404


class TestRateLimit:
    async def test_the_twenty_first_check_in_a_minute_is_429(self, redis, vault_on):
        check = AsyncMock(
            return_value=await vault_diagnostics.check_reference(REF, cfg=_settings(enabled=False))
        )
        app = _app(_user())
        codes = []
        with _patch_caps("write"), patch.object(vault_diagnostics, "check_reference", check):
            for _ in range(vault_diagnostics.CHECKS_PER_MINUTE + 1):
                app.dependency_overrides[get_db] = lambda: _db(_ws())
                resp = await _call(app, "POST", WS_PATH, json=_body({"reference": REF}))
                codes.append(resp.status_code)
        assert codes[:-1] == [200] * vault_diagnostics.CHECKS_PER_MINUTE
        assert codes[-1] == 429
        assert int(resp.headers["retry-after"]) > 0
        assert check.await_count == vault_diagnostics.CHECKS_PER_MINUTE

    async def test_the_limit_is_per_user(self, redis, vault_on):
        with patch.object(vault_diagnostics, "check_reference", AsyncMock(return_value={})):
            for _ in range(vault_diagnostics.CHECKS_PER_MINUTE):
                await vault_diagnostics.check_rate_allowed("a@example.com")
        allowed_a, _ = await vault_diagnostics.check_rate_allowed("a@example.com")
        allowed_b, _ = await vault_diagnostics.check_rate_allowed("b@example.com")
        assert allowed_a is False and allowed_b is True

    async def test_an_unauthorised_caller_does_not_spend_the_budget(self, redis, vault_on):
        with _patch_caps("read"):
            await _call(_app(_user(), _db(_ws())), "POST", WS_PATH, json=_body({"reference": REF}))
        assert redis.counters == {}

    async def test_redis_down_fails_open(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            assert await vault_diagnostics.check_rate_allowed("a@example.com") == (True, 0)


# ── POST /varsets/{id}/vault-reference-checks ────────────────────────────


VS_PATH = f"/api/terrapod/v1/varsets/varset-{VS_ID}/vault-reference-checks"


class TestVarsetCheck:
    async def test_non_admins_are_refused(self, redis, vault_on):
        resp = await _call(
            _app(_user(["everyone"])), "POST", VS_PATH, json=_body({"reference": REF})
        )
        assert resp.status_code == 403

    async def test_an_admin_checks_a_reference(self, redis, vault_on):
        vs = MagicMock(id=VS_ID)
        _seen, patched = _fake_vault()
        with patched:
            resp = await _call(
                _app(_user(["admin"]), _db(vs)), "POST", VS_PATH, json=_body({"reference": REF})
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["keys"] == ["token"]
        assert SECRET not in resp.text

    async def test_an_unknown_varset_is_404(self, redis, vault_on):
        resp = await _call(
            _app(_user(["admin"]), _db(None)), "POST", VS_PATH, json=_body({"reference": REF})
        )
        assert resp.status_code == 404

    async def test_a_stored_varset_variable_is_checked(self, redis, vault_on):
        vs = MagicMock(id=VS_ID)
        vsv = MagicMock(key="TOKEN", value=json.dumps(REF), value_source="vault")
        _seen, patched = _fake_vault()
        with patched:
            resp = await _call(
                _app(_user(["admin"]), _db(vs, vsv)),
                "POST",
                VS_PATH,
                json=_body({"variable-id": f"var-{uuid.uuid4()}"}),
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["ok"] is True
