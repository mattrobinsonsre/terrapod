"""Vault diagnostics service tests (#1663).

Vault is driven through ``httpx.MockTransport`` so the real request handling
runs — URLs, headers, the health body, the capabilities answer — rather than
patching the functions under test. Redis is an in-memory fake.

The properties that matter most, and each has a test that fails if it breaks:

- a dynamic engine is never read (a read mints a credential);
- a kv-v2 key listing carries key names, never a value;
- the status endpoint's reader never contacts Vault;
- recording a resolution failure never changes a claim's outcome.
"""

import ast
import inspect
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.config import Settings, VaultConfig, VaultInstanceConfig
from terrapod.services import vault_client, vault_diagnostics
from terrapod.services.vault_client import reset_token_cache
from terrapod.services.vault_diagnostics import (
    FAIL,
    NOTE_DYNAMIC_NOT_READ,
    NOTE_KEYS_NEED_PLAN,
    NOTE_LOCAL_EXECUTION,
    NOTE_VAULT_DISABLED,
    PASS,
    SKIPPED,
    UNKNOWN,
    acl_path,
    check_reference,
    probe_instance,
    read_status,
    record_resolution_error,
    referenced_fields,
    required_capabilities,
    tls_trust,
)

#: A value no response, message or stored record may ever contain.
SECRET = "S3CR3T-VALUE-MUST-NOT-LEAK"


def _inst(**kw) -> VaultInstanceConfig:
    base = {
        "name": "default",
        "address": "https://vault.test:8200",
        "auth": {"method": "token", "mount": "token", "role": "terrapod"},
    }
    base.update(kw)
    return VaultInstanceConfig(**base)


def _settings(*instances, enabled=True) -> Settings:
    s = Settings()
    s.vault = VaultConfig(enabled=enabled, instances=list(instances) or [_inst()])
    return s


class FakeVault:
    """A scripted Vault. Routes by path; records every request it sees."""

    def __init__(
        self,
        *,
        health=None,
        login=(200, {"auth": {"client_token": "s.tok", "lease_duration": 3600}}),
        lookup=(200, {"data": {"ttl": 3599}}),
        caps=None,
        kv=None,
        unreachable=False,
    ):
        self.health = health or (
            200,
            {"initialized": True, "sealed": False, "standby": False, "version": "1.18.0"},
        )
        self.login = login
        self.lookup = lookup
        self.caps = caps if caps is not None else (200, {"capabilities": ["read"]})
        self.kv = kv if kv is not None else (200, {"data": {"data": {"token": SECRET}}})
        self.unreachable = unreachable
        self.seen: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [r.url.path for r in self.seen]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        p = request.url.path
        if p == "/v1/sys/health":
            status, body = self.health
        elif p.startswith("/v1/auth/") and p.endswith("/login"):
            status, body = self.login
        elif p == "/v1/auth/token/lookup-self":
            status, body = self.lookup
        elif p == "/v1/sys/capabilities-self":
            status, body = self.caps
        else:
            status, body = self.kv
        return httpx.Response(status, json=body)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched(fake):
    def factory(*_a, **_kw):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(fake))

    # vault_client.httpx and vault_diagnostics.httpx are the same module, so
    # this routes both the resolver's and the diagnostics' clients.
    return patch.object(vault_client.httpx, "AsyncClient", factory)


class FakeRedis:
    def __init__(self):
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.counters: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex:
            self.ttls[key] = ex

    async def get(self, key):
        return self.strings.get(key)

    def pipeline(self, transaction=False):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def incr(self, key):
        self.ops.append(("incr", key))

    def expire(self, key, seconds):
        self.ops.append(("expire", key))

    async def execute(self):
        out = []
        for op, key in self.ops:
            if op == "incr":
                self.r.counters[key] = self.r.counters.get(key, 0) + 1
                out.append(self.r.counters[key])
            else:
                out.append(True)
        return out


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_token_cache()
    yield
    reset_token_cache()


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        yield fake


@pytest.fixture
def static_secret(monkeypatch):
    monkeypatch.setenv("TERRAPOD_VAULT_DEFAULT_SECRET", "s.static")


@pytest.fixture
def sa_token():
    with patch.object(vault_client, "_read_sa_token", AsyncMock(return_value="jwt.sa.token")):
        yield


# ── TLS trust ────────────────────────────────────────────────────────────


class TestTlsTrust:
    def test_skip_verify(self):
        assert tls_trust(_inst(tls_skip_verify=True)) == "skip-verify"

    def test_instance_ca(self):
        assert tls_trust(_inst(ca_file="/etc/vault-ca/ca.crt")) == "instance-ca"

    def test_global_bundle_when_ssl_cert_file_is_set(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/bundle.pem")
        assert tls_trust(_inst()) == "global-bundle"

    def test_default_store_otherwise(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        assert tls_trust(_inst()) == "default"

    def test_an_instance_ca_wins_over_the_global_bundle(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/bundle.pem")
        assert tls_trust(_inst(ca_file="/ca.crt")) == "instance-ca"


# ── Status sampling ──────────────────────────────────────────────────────


class TestProbeEachAuthMethod:
    async def test_token_auth_is_proved_by_the_lookup(self, static_secret):
        fake = FakeVault()
        with _patched(fake):
            got = await probe_instance(_inst())
        assert got["reachable"] is True and got["sealed"] is False
        assert got["version"] == "1.18.0"
        assert got["login-ok"] is True
        assert got["ttl-seconds"] == 3599
        # A static token has no login call; the lookup is the proof.
        assert not any(p.endswith("/login") for p in fake.paths())
        assert fake.seen[-1].headers["X-Vault-Token"] == "s.static"

    async def test_approle_logs_in_with_role_and_secret_id(self, static_secret):
        fake = FakeVault()
        inst = _inst(auth={"method": "approle", "mount": "approle", "role": "role-id-1"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["login-ok"] is True and got["ttl-seconds"] == 3599
        login = next(r for r in fake.seen if r.url.path == "/v1/auth/approle/login")
        assert json.loads(login.content) == {"role_id": "role-id-1", "secret_id": "s.static"}

    async def test_kubernetes_presents_the_service_account_token(self, sa_token):
        fake = FakeVault()
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["login-ok"] is True
        login = next(r for r in fake.seen if r.url.path == "/v1/auth/kubernetes/login")
        assert json.loads(login.content) == {"role": "terrapod", "jwt": "jwt.sa.token"}

    async def test_jwt_logs_in_at_the_jwt_mount(self, sa_token):
        fake = FakeVault()
        inst = _inst(auth={"method": "jwt", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["login-ok"] is True
        assert "/v1/auth/jwt/login" in fake.paths()

    async def test_the_namespace_is_sent_on_login_but_not_on_health(self, sa_token):
        fake = FakeVault()
        inst = _inst(namespace="admin", auth={"method": "jwt", "role": "terrapod"})
        with _patched(fake):
            await probe_instance(inst)
        health = next(r for r in fake.seen if r.url.path == "/v1/sys/health")
        login = next(r for r in fake.seen if r.url.path.endswith("/login"))
        assert "X-Vault-Namespace" not in health.headers
        assert login.headers["X-Vault-Namespace"] == "admin"


class TestProbeFailures:
    async def test_login_denied_is_reported_with_the_resolvers_message(self, sa_token):
        fake = FakeVault(login=(403, {"errors": ["permission denied"]}))
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["reachable"] is True
        assert got["login-ok"] is False
        assert "HTTP 403" in got["login-error"] and "role 'terrapod'" in got["login-error"]

    async def test_a_rejected_static_token_is_a_failed_login(self, static_secret):
        fake = FakeVault(lookup=(403, {"errors": ["permission denied"]}))
        with _patched(fake):
            got = await probe_instance(_inst())
        assert got["login-ok"] is False
        assert "rejected the static token" in got["login-error"]

    async def test_a_refused_lookup_after_a_real_login_only_loses_the_ttl(self, sa_token):
        fake = FakeVault(lookup=(403, {}))
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["login-ok"] is True
        assert got["ttl-seconds"] is None
        assert "TTL is unknown" in got["login-error"]

    async def test_a_refused_lookup_drops_the_cached_token_and_logs_in_again(self, sa_token):
        fake = FakeVault(lookup=(403, {}))
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            await probe_instance(inst)
        assert fake.paths().count("/v1/auth/kubernetes/login") == 2

    async def test_unreachable_is_not_a_login_failure(self, sa_token):
        fake = FakeVault(unreachable=True)
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["reachable"] is False
        assert "ConnectError" in got["health-error"]
        # Not attempted, so unknown — never "false".
        assert got["login-ok"] is None
        assert fake.paths() == ["/v1/sys/health"]

    async def test_a_sealed_vault_is_reported_and_no_login_is_attempted(self, sa_token):
        fake = FakeVault(
            health=(200, {"initialized": True, "sealed": True, "standby": False, "version": "1.18"})
        )
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(fake):
            got = await probe_instance(inst)
        assert got["reachable"] is True and got["sealed"] is True
        assert got["login-ok"] is None
        assert fake.paths() == ["/v1/sys/health"]

    async def test_a_standby_node_is_reported(self, static_secret):
        fake = FakeVault(
            health=(200, {"initialized": True, "sealed": False, "standby": True, "version": "1"})
        )
        with _patched(fake):
            got = await probe_instance(_inst())
        assert got["standby"] is True and got["login-ok"] is True

    async def test_a_non_json_health_answer_is_reachable_with_an_error(self, static_secret):
        def handler(request):
            return httpx.Response(502, text="<html>bad gateway</html>")

        def factory(*_a, **_kw):
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

        with patch.object(vault_client.httpx, "AsyncClient", factory):
            got = await probe_instance(_inst())
        assert got["reachable"] is True
        assert "502" in got["health-error"]

    async def test_a_probe_never_raises(self):
        with patch.object(vault_diagnostics, "_health", AsyncMock(side_effect=RuntimeError("x"))):
            got = await probe_instance(_inst())
        assert got["name"] == "default"
        assert "RuntimeError" in got["health-error"]


class TestSampleAndRead:
    async def test_a_sample_is_stored_and_read_back(self, redis, static_secret):
        s = _settings()
        with _patched(FakeVault()):
            await vault_diagnostics.sample(s)
        got = await read_status(s)
        assert got["enabled"] is True and got["sampled-at"]
        (inst,) = got["instances"]
        assert inst["name"] == "default"
        assert inst["reachable"] is True and inst["login-ok"] is True
        assert inst["ttl-seconds"] == 3599
        assert inst["auth-method"] == "token"
        assert redis.ttls[vault_diagnostics.STATUS_KEY] == vault_diagnostics.STATUS_TTL

    async def test_reading_never_contacts_vault(self, redis):
        """The request path reads Redis only — no health, no login."""
        s = _settings()

        def boom(*_a, **_kw):
            raise AssertionError("read_status must not open an HTTP client")

        with (
            patch.object(vault_client.httpx, "AsyncClient", boom),
            patch.object(vault_client, "_login", AsyncMock(side_effect=AssertionError)),
        ):
            got = await read_status(s)
        assert got["instances"][0]["name"] == "default"

    async def test_an_unsampled_instance_is_listed_with_unknown_health(self, redis):
        got = await read_status(_settings())
        assert got["unavailable-reason"] == "not sampled yet"
        (inst,) = got["instances"]
        assert inst["reachable"] is None and inst["login-ok"] is None
        assert inst["checked-at"] is None

    async def test_disabled_is_an_empty_list(self, redis):
        got = await read_status(_settings(enabled=False))
        assert got == {
            "enabled": False,
            "sampled-at": None,
            "unavailable-reason": None,
            "instances": [],
        }

    async def test_redis_down_is_reported_not_raised(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            got = await read_status(_settings())
        assert got["unavailable-reason"] == "cache unreachable"
        assert got["instances"][0]["reachable"] is None

    async def test_the_last_error_is_merged_into_its_instance(self, redis):
        await record_resolution_error(
            "default", vault_client.VaultDenied("x"), message="variable 'A': denied"
        )
        got = await read_status(_settings())
        err = got["instances"][0]["last-error"]
        assert err["class"] == "VaultDenied"
        assert err["message"] == "variable 'A': denied"
        assert err["at"].endswith("Z")

    async def test_sample_cycle_does_nothing_when_vault_is_off(self, redis):
        probe = AsyncMock()
        with (
            patch.object(vault_diagnostics, "settings", _settings(enabled=False)),
            patch.object(vault_diagnostics, "probe_instance", probe),
        ):
            await vault_diagnostics.sample_cycle()
        probe.assert_not_called()
        assert vault_diagnostics.STATUS_KEY not in redis.strings

    async def test_sample_cycle_never_raises(self):
        with (
            patch.object(vault_diagnostics, "settings", _settings()),
            patch.object(vault_diagnostics, "sample", AsyncMock(side_effect=RuntimeError("x"))),
        ):
            await vault_diagnostics.sample_cycle()


# ── Last resolution error ────────────────────────────────────────────────


class TestRecordResolutionError:
    async def test_names_the_class_and_message(self, redis):
        await record_resolution_error("default", vault_client.VaultUnavailable("sealed 503"))
        stored = json.loads(redis.strings["tp:vault:last_error:default"])
        assert stored["class"] == "VaultUnavailable"
        assert stored["message"] == "sealed 503"
        assert redis.ttls["tp:vault:last_error:default"] == vault_diagnostics.LAST_ERROR_TTL

    async def test_a_raising_redis_write_is_swallowed(self):
        broken = AsyncMock()
        broken.set.side_effect = ConnectionError("redis gone")
        with patch("terrapod.redis.client.get_redis_client", return_value=broken):
            await record_resolution_error("default", RuntimeError("x"))

    async def test_no_redis_at_all_is_swallowed(self):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("down")):
            await record_resolution_error("default", RuntimeError("x"))

    async def test_the_message_is_bounded(self, redis):
        await record_resolution_error("default", RuntimeError("y" * 5000))
        stored = json.loads(redis.strings["tp:vault:last_error:default"])
        assert len(stored["message"]) == 500


class _Var:
    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.value_source = "vault"
        self.hcl = False


class TestTheClaimPathRecordsFailuresBestEffort:
    """A failed resolution is recorded, and a broken Redis never changes the outcome."""

    def _ref(self):
        return json.dumps({"mount": "secret", "path": "apps/x", "field": "token"})

    async def test_a_denied_read_is_recorded(self, redis, static_secret):
        from terrapod.services.vault_source_service import (
            VaultSourceError,
            resolve_vault_delivery,
        )

        with _patched(FakeVault(kv=(403, {}))), pytest.raises(VaultSourceError):
            await resolve_vault_delivery([_Var("A", self._ref())], _settings())
        stored = json.loads(redis.strings["tp:vault:last_error:default"])
        assert stored["class"] == "VaultDenied"
        assert "variable 'A'" in stored["message"]

    async def test_a_raising_redis_write_never_fails_or_changes_a_claim(self, static_secret):
        from terrapod.services.vault_source_service import (
            VaultSourceError,
            VaultTransient,
            resolve_vault_delivery,
        )

        broken = AsyncMock()
        broken.set.side_effect = ConnectionError("redis gone")
        with patch("terrapod.redis.client.get_redis_client", return_value=broken):
            # A denied read still fails the run for Vault's reason ...
            with _patched(FakeVault(kv=(403, {}))), pytest.raises(VaultSourceError) as e:
                await resolve_vault_delivery([_Var("A", self._ref())], _settings())
            assert not isinstance(e.value, VaultTransient)
            assert "denied" in str(e.value)
            # ... a transient one still leaves the run queued ...
            with _patched(FakeVault(kv=(503, {}))), pytest.raises(VaultTransient):
                await resolve_vault_delivery([_Var("A", self._ref())], _settings())
            # ... and a good one still resolves.
            with _patched(FakeVault()):
                got = await resolve_vault_delivery([_Var("A", self._ref())], _settings())
        assert got.values == {"A": SECRET}

    async def test_even_a_raising_recorder_never_fails_a_claim(self, static_secret):
        from terrapod.services.vault_source_service import (
            VaultSourceError,
            resolve_vault_delivery,
        )

        with (
            patch.object(
                vault_diagnostics,
                "record_resolution_error",
                AsyncMock(side_effect=RuntimeError("diagnostics broke")),
            ),
            _patched(FakeVault(kv=(404, {}))),
            pytest.raises(VaultSourceError) as e,
        ):
            await resolve_vault_delivery([_Var("A", self._ref())], _settings())
        assert "no secret" in str(e.value)

    async def test_a_good_claim_records_nothing(self, redis, static_secret):
        from terrapod.services.vault_source_service import resolve_vault_delivery

        with _patched(FakeVault()):
            await resolve_vault_delivery([_Var("A", self._ref())], _settings())
        assert "tp:vault:last_error:default" not in redis.strings


# ── Pure helpers ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_acl_path_names_the_kv2_data_segment(self):
        assert acl_path({"mount": "secret", "path": "apps/x"}) == "secret/data/apps/x"

    def test_acl_path_for_a_dynamic_engine_is_the_path_itself(self):
        ref = {"mount": "database", "path": "creds/ro", "engine": "dynamic"}
        assert acl_path(ref) == "database/creds/ro"

    def test_a_get_needs_read(self):
        assert required_capabilities({"engine": "dynamic"}) == ["read"]
        assert required_capabilities({}) == ["read"]

    def test_a_dynamic_post_needs_update_or_create(self):
        ref = {"engine": "dynamic", "method": "post"}
        assert required_capabilities(ref) == ["update", "create"]

    def test_a_kv2_reference_is_always_a_read_whatever_its_method(self):
        assert required_capabilities({"method": "POST"}) == ["read"]

    def test_referenced_fields_from_a_field(self):
        assert referenced_fields({"field": "token"}) == ["token"]

    def test_referenced_fields_from_a_template_take_tag_roots_not_lease(self):
        ref = {
            "file": {
                "template": "{{ access_key }}\n{{ nested.inner | trim }}\n{{ _lease.ttl }}\n"
                "{{ access_key }}"
            }
        }
        assert referenced_fields(ref) == ["access_key", "nested"]

    def test_referenced_fields_from_a_format(self):
        assert referenced_fields({"file": {"format": "env", "fields": ["a", "b"]}}) == ["a", "b"]

    def test_a_whole_secret_format_names_no_field(self):
        assert referenced_fields({"file": {"format": "json"}}) == []


# ── Reference check ──────────────────────────────────────────────────────


def _statuses(result):
    return {c["name"]: c["status"] for c in result["checks"]}


KV_REF = {"mount": "secret", "path": "apps/x", "field": "token"}


class TestReferenceCheck:
    async def test_a_reference_that_does_not_parse_stops_there(self):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference({"mount": "secret"}, cfg=_settings())
        assert got["parses"] is False
        assert "missing: path, field" in got["parse-error"]
        assert _statuses(got) == {"parses": FAIL}
        assert fake.seen == []

    async def test_a_bad_file_block_is_a_parse_failure(self):
        ref = {**KV_REF, "file": {"mode": "0600"}}
        got = await check_reference(ref, cfg=_settings())
        assert got["parses"] is False and "reserved" in got["parse-error"]

    async def test_a_stored_string_is_accepted(self, static_secret):
        with _patched(FakeVault()):
            got = await check_reference(json.dumps(KV_REF), cfg=_settings())
        assert got["parses"] is True and got["ok"] is True

    async def test_vault_disabled(self):
        got = await check_reference(KV_REF, cfg=_settings(enabled=False))
        assert got["vault-enabled"] is False
        assert NOTE_VAULT_DISABLED in got["notes"]
        assert _statuses(got)["instance"] == FAIL

    async def test_an_unknown_instance(self):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference({**KV_REF, "vault": "nope"}, cfg=_settings())
        assert got["instance-known"] is False
        assert "unknown vault instance 'nope'" in got["checks"][-1]["detail"]
        assert fake.seen == []

    async def test_no_default_among_several_instances(self):
        s = _settings(_inst(name="a"), _inst(name="b"))
        got = await check_reference(KV_REF, cfg=s)
        assert got["instance-known"] is False
        assert "none is marked default" in got["checks"][-1]["detail"]

    async def test_a_path_outside_the_allow_list_never_reaches_vault(self):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference(
                {**KV_REF, "path": "other/x"}, cfg=_settings(_inst(paths=["secret/apps"]))
            )
        assert got["path-allowed"] is False
        assert "allow-list" in got["checks"][-1]["detail"]
        assert fake.seen == []

    async def test_a_traversal_is_refused_as_outside_the_allow_list(self):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference(
                {**KV_REF, "path": "apps/../../sys/mounts"},
                cfg=_settings(_inst(paths=["secret/apps"])),
            )
        assert got["path-allowed"] is False and fake.seen == []

    async def test_capabilities_denied(self, static_secret):
        fake = FakeVault(caps=(200, {"capabilities": ["list"]}))
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings())
        assert got["readable"] is False
        assert got["capabilities"] == ["list"]
        assert "needs read" in got["checks"][-1]["detail"]
        # Denied: the secret is never read.
        assert "/v1/secret/data/apps/x" not in fake.paths()

    async def test_an_explicit_deny_wins(self, static_secret):
        fake = FakeVault(caps=(200, {"capabilities": ["deny"]}))
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings())
        assert got["readable"] is False

    async def test_capabilities_are_asked_about_the_policy_path(self, static_secret):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings())
        req = next(r for r in fake.seen if r.url.path == "/v1/sys/capabilities-self")
        assert json.loads(req.content) == {"paths": ["secret/data/apps/x"]}
        assert got["read-path"] == "secret/data/apps/x"

    async def test_the_newer_per_path_capabilities_shape_is_read(self, static_secret):
        fake = FakeVault(
            caps=(200, {"data": {"secret/data/apps/x": ["read", "list"]}}),
        )
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings())
        assert got["readable"] is True and got["capabilities"] == ["read", "list"]

    async def test_a_capabilities_403_is_a_failed_readable(self, static_secret):
        with _patched(FakeVault(caps=(403, {}))):
            got = await check_reference(KV_REF, cfg=_settings())
        assert got["readable"] is False
        assert "sys/capabilities-self" in got["checks"][-1]["detail"]

    async def test_a_sealed_vault_makes_readable_unknown(self, static_secret):
        with _patched(FakeVault(caps=(503, {}))):
            got = await check_reference(KV_REF, cfg=_settings())
        assert _statuses(got)["readable"] == UNKNOWN
        assert got["readable"] is None and got["ok"] is False

    async def test_a_login_failure_is_reported_on_readable(self, sa_token):
        inst = _inst(auth={"method": "kubernetes", "role": "terrapod"})
        with _patched(FakeVault(login=(403, {}))):
            got = await check_reference(KV_REF, cfg=_settings(inst))
        assert got["readable"] is False
        assert "Vault login failed" in got["checks"][-1]["detail"]

    async def test_kv2_lists_key_names_and_confirms_the_field(self, static_secret):
        fake = FakeVault(kv=(200, {"data": {"data": {"token": SECRET, "user": SECRET + "2"}}}))
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings())
        assert got["ok"] is True
        assert got["keys"] == ["token", "user"]
        assert got["fields-present"] is True and got["missing-fields"] == []
        assert _statuses(got) == {
            "parses": PASS,
            "instance": PASS,
            "path-allowed": PASS,
            "readable": PASS,
            "fields-present": PASS,
        }

    async def test_no_value_ever_appears_in_the_result(self, static_secret):
        """The key listing carries names only. A value anywhere is a leak."""
        kv = {"token": SECRET, "nested": {"inner": SECRET}, "list": [SECRET]}
        fake = FakeVault(kv=(200, {"data": {"data": kv}}))
        for ref in (
            KV_REF,
            {**KV_REF, "field": "absent"},
            {"mount": "secret", "path": "apps/x", "file": {"format": "json"}},
            {"mount": "secret", "path": "apps/x", "file": {"template": "{{ token }}"}},
        ):
            with _patched(fake):
                got = await check_reference(ref, cfg=_settings())
            assert SECRET not in json.dumps(got), ref
            assert got["keys"] == ["list", "nested", "token"]

    async def test_a_missing_field_is_named(self, static_secret):
        with _patched(FakeVault()):
            got = await check_reference({**KV_REF, "field": "password"}, cfg=_settings())
        assert got["ok"] is False
        assert got["fields-present"] is False and got["missing-fields"] == ["password"]
        assert got["checks"][-1]["detail"] == "not present: password"

    async def test_template_fields_are_checked(self, static_secret):
        ref = {
            "mount": "secret",
            "path": "apps/x",
            "file": {"template": "{{ token }} {{ secret_key }} {{ _lease.ttl }}"},
        }
        with _patched(FakeVault()):
            got = await check_reference(ref, cfg=_settings())
        assert got["missing-fields"] == ["secret_key"]

    async def test_format_fields_are_checked(self, static_secret):
        ref = {"mount": "secret", "path": "apps/x", "file": {"format": "env", "fields": ["token"]}}
        with _patched(FakeVault()):
            got = await check_reference(ref, cfg=_settings())
        assert got["fields-present"] is True and got["ok"] is True

    async def test_a_dynamic_engine_is_never_read(self, static_secret):
        """Every read of a dynamic engine mints a credential. A check must not."""
        fake = FakeVault(kv=(200, {"data": {"username": "minted", "password": SECRET}}))
        ref = {"mount": "database", "path": "creds/ro", "field": "password", "engine": "dynamic"}
        with _patched(fake):
            got = await check_reference(ref, cfg=_settings())
        assert fake.paths() == ["/v1/sys/capabilities-self"]
        assert got["keys"] is None and got["fields-present"] is None
        assert NOTE_DYNAMIC_NOT_READ in got["notes"]
        assert _statuses(got)["fields-present"] == SKIPPED
        assert got["ok"] is True

    async def test_a_dynamic_post_is_never_sent(self, static_secret):
        fake = FakeVault(caps=(200, {"capabilities": ["update"]}))
        ref = {
            "mount": "pki",
            "path": "issue/web",
            "engine": "dynamic",
            "method": "POST",
            "data": {"common_name": "a.example"},
            "file": {"template": "{{ certificate }}"},
        }
        with _patched(fake):
            got = await check_reference(ref, cfg=_settings())
        assert fake.paths() == ["/v1/sys/capabilities-self"]
        assert got["readable"] is True
        assert got["required-capabilities"] == ["update", "create"]

    async def test_without_plan_permission_keys_are_not_listed(self, static_secret):
        fake = FakeVault()
        with _patched(fake):
            got = await check_reference(KV_REF, cfg=_settings(), may_list_keys=False)
        assert "/v1/secret/data/apps/x" not in fake.paths()
        assert got["keys"] is None
        assert NOTE_KEYS_NEED_PLAN in got["notes"]
        assert got["readable"] is True

    async def test_a_local_workspace_is_noted(self):
        got = await check_reference(KV_REF, cfg=_settings(enabled=False), local_execution=True)
        assert NOTE_LOCAL_EXECUTION in got["notes"]

    async def test_a_kv2_read_that_now_404s_fails_fields_present(self, static_secret):
        with _patched(FakeVault(kv=(404, {}))):
            got = await check_reference(KV_REF, cfg=_settings())
        assert _statuses(got)["fields-present"] == FAIL
        assert "no secret" in got["checks"][-1]["detail"]


# ── Source introspection ─────────────────────────────────────────────────


class TestTheCheckNeverReadsADynamicEngine:
    """Pins the structure behind 'a dynamic engine is never read' (#1663).

    The behavioural test above proves it for the cases it tries; this proves it
    for every path, by reading the source: the only secret read in the module
    is one call, in one function, that hard-codes ``engine="kv2"``, and the
    function that calls it does so only on the kv-v2 branch.
    """

    TREE = ast.parse(inspect.getsource(vault_diagnostics))
    READERS = {"read_secret_response", "read_secret_data", "read_secret"}

    def _calls(self, node, name):
        return [
            c
            for c in ast.walk(node)
            if isinstance(c, ast.Call)
            and (
                (isinstance(c.func, ast.Name) and c.func.id == name)
                or (isinstance(c.func, ast.Attribute) and c.func.attr == name)
            )
        ]

    def _fn(self, name):
        return next(
            n
            for n in ast.walk(self.TREE)
            if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == name
        )

    def test_exactly_one_secret_read_and_it_hardcodes_kv2(self):
        calls = [c for r in self.READERS for c in self._calls(self.TREE, r)]
        assert len(calls) == 1, "the check may read a secret in exactly one place"
        (call,) = calls
        assert call.func.id == "read_secret_response"
        engine = next(k for k in call.keywords if k.arg == "engine")
        assert isinstance(engine.value, ast.Constant) and engine.value.value == "kv2"
        assert call in self._calls(self._fn("_kv2_key_names"), "read_secret_response")

    def test_the_key_listing_is_reached_only_on_the_kv2_branch(self):
        check = self._fn("check_reference")
        (call,) = self._calls(check, "_kv2_key_names")
        guard = next(
            n
            for n in ast.walk(check)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and isinstance(n.test.ops[0], ast.NotEq)
            and isinstance(n.test.comparators[0], ast.Constant)
            and n.test.comparators[0].value == "kv2"
        )
        in_body = any(call in list(ast.walk(stmt)) for stmt in guard.body)
        in_else = any(call in list(ast.walk(stmt)) for stmt in guard.orelse)
        assert not in_body and in_else, "the key listing must sit behind `engine != 'kv2'`"

    def test_the_key_listing_returns_names_only(self):
        """``_kv2_key_names`` reduces the data map to its keys before returning."""
        fn = self._fn("_kv2_key_names")
        (ret,) = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        src = ast.unparse(ret.value)
        assert src == "sorted((str(k) for k in response.data))"
