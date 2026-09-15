"""Vault client unit tests (#1439).

Driven through `httpx.MockTransport` so the real request/response handling runs
— URL construction, the kv-v2 `data.data` nesting, headers, status handling —
rather than patching the client and asserting on a mock.

The live proof against a real Vault in-cluster is separate and does not replace
these: it cannot exercise every error branch, and it does not run in CI.
"""

import os
import ssl
from unittest.mock import patch

import httpx
import pytest

from terrapod.config import VaultInstanceConfig
from terrapod.services import vault_client
from terrapod.services.vault_client import (
    VaultError,
    VaultUnavailable,
    read_secret,
    reset_token_cache,
)


def _inst(**kw) -> VaultInstanceConfig:
    base = {
        "name": "default",
        "address": "https://vault.test:8200",
        "auth": {"method": "token", "mount": "token", "role": "n/a"},
    }
    base.update(kw)
    return VaultInstanceConfig(**base)


class _Recorder:
    """Captures the requests the client makes, and replies from a script."""

    def __init__(self, replies):
        self.replies = replies
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        # Sticky on the last scripted reply rather than falling back to 200.
        # The retry helper re-issues on 5xx, and a synthetic success after the
        # script ran out made a failure test pass for the wrong reason.
        if self.replies:
            self._last = self.replies.pop(0)
        status, body = getattr(self, "_last", (200, {}))
        return httpx.Response(status, json=body)


#: Captured before patching — the factory below replaces the name the module
#: looks up, so calling httpx.AsyncClient inside it would recurse for ever.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched(rec):
    """Route every AsyncClient the module builds through the recorder."""

    def factory(*_a, **_kw):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(rec))

    return patch.object(vault_client.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retry without waiting. The retry itself is what these tests exercise;
    the real backoff made each failure-path test sleep for seven seconds."""
    from terrapod import http_retry

    monkeypatch.setattr(http_retry, "_backoff_seconds", lambda *_a, **_k: 0)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_token_cache()
    yield
    reset_token_cache()


class TestNonStringFieldsAreJsonEncoded:
    """A map or list field is delivered as JSON, not a Python repr (#1619).

    Matters most for file delivery: a service-account key stored in Vault as an
    object must land on disk as a JSON document a provider can parse.
    """

    async def _read(self, value):
        rec = _Recorder([(200, {"data": {"data": {"f": value}}})])
        with _patched(rec):
            return await read_secret(_inst(), mount="kvv2", path="a", field="f", static_token="t")

    @pytest.mark.asyncio
    async def test_a_string_is_returned_unchanged(self):
        assert await self._read("plain 'quoted' {not json}") == "plain 'quoted' {not json}"

    @pytest.mark.asyncio
    async def test_a_dict_is_json(self):
        import json

        got = await self._read({"type": "service_account", "enabled": True, "n": None})
        assert json.loads(got) == {"type": "service_account", "enabled": True, "n": None}
        assert "'" not in got and "True" not in got and "None" not in got

    @pytest.mark.asyncio
    async def test_a_list_is_json(self):
        import json

        got = await self._read(["a", 1, False])
        assert json.loads(got) == ["a", 1, False]
        assert got == '["a", 1, false]'

    @pytest.mark.asyncio
    async def test_a_nested_structure_round_trips(self):
        import json

        doc = {"outer": {"inner": [1, {"k": "v"}], "empty": {}}, "list": [[], [None]]}
        assert json.loads(await self._read(doc)) == doc

    @pytest.mark.asyncio
    async def test_non_ascii_is_kept_as_characters_not_escapes(self):
        import json

        doc = {"name": "café ☕ 日本", "list": ["ü"]}
        got = await self._read(doc)
        assert json.loads(got) == doc
        assert "café ☕ 日本" in got and "\\u" not in got

    @pytest.mark.asyncio
    async def test_scalars_other_than_str_keep_their_previous_rendering(self):
        """Only maps and lists changed; numbers and booleans are as before."""
        assert await self._read(5) == "5"
        assert await self._read(True) == "True"


class TestReadShapes:
    @pytest.mark.asyncio
    async def test_kv2_unwraps_the_nested_data(self):
        # kv-v2 nests the secret under data.data. Reading data directly is the
        # classic mistake and would return the metadata envelope instead.
        rec = _Recorder([(200, {"data": {"data": {"apitoken": "s3cr3t"}, "metadata": {}}})])
        with _patched(rec):
            got = await read_secret(
                _inst(), mount="kvv2", path="apps/netbox", field="apitoken", static_token="t"
            )
        assert got == "s3cr3t"
        assert rec.seen[0].url.path == "/v1/kvv2/data/apps/netbox"
        assert rec.seen[0].headers["X-Vault-Token"] == "t"

    @pytest.mark.asyncio
    async def test_dynamic_reads_the_path_directly(self):
        """A dynamic engine has no `data/` segment — that is kv-v2's own layout."""
        rec = _Recorder([(200, {"data": {"access_key": "AKIA", "secret_key": "shh"}})])
        with _patched(rec):
            got = await read_secret(
                _inst(),
                mount="aws",
                path="creds/deploy",
                field="secret_key",
                engine="dynamic",
                static_token="t",
            )
        assert got == "shh"
        assert rec.seen[0].url.path == "/v1/aws/creds/deploy"

    @pytest.mark.asyncio
    async def test_dynamic_post_sends_the_body(self):
        """pki/issue and aws/sts are writes, not reads."""
        rec = _Recorder([(200, {"data": {"certificate": "-----BEGIN"}})])
        with _patched(rec):
            got = await read_secret(
                _inst(),
                mount="pki",
                path="issue/example",
                field="certificate",
                engine="dynamic",
                method="POST",
                data={"common_name": "a.example.test"},
                static_token="t",
            )
        assert got.startswith("-----BEGIN")
        assert rec.seen[0].method == "POST"
        assert b"a.example.test" in rec.seen[0].content

    @pytest.mark.asyncio
    async def test_kv2_ignores_a_post_method(self):
        """A kv-v2 read is a GET whatever the reference claims."""
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            await read_secret(
                _inst(), mount="kvv2", path="a", field="k", method="POST", static_token="t"
            )
        assert rec.seen[0].method == "GET"

    @pytest.mark.asyncio
    async def test_namespace_header_is_sent_when_configured(self):
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            await read_secret(
                _inst(namespace="team-a"), mount="kvv2", path="a", field="k", static_token="t"
            )
        assert rec.seen[0].headers["X-Vault-Namespace"] == "team-a"


class TestFailuresAreLoudAndSpecific:
    """Every failure names what to fix. A credential that resolves to nothing is
    worse than one that fails, so none of these may return quietly."""

    @pytest.mark.asyncio
    async def test_403_blames_the_policy(self):
        rec = _Recorder([(403, {"errors": ["permission denied"]})])
        with _patched(rec), pytest.raises(VaultError, match="policy attached to role"):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    async def test_404_names_the_path(self):
        rec = _Recorder([(404, {})])
        with _patched(rec), pytest.raises(VaultError, match="no secret at 'kvv2/a'"):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    async def test_a_missing_field_lists_what_is_there(self):
        # The likeliest operator error, so the message has to be actionable.
        rec = _Recorder([(200, {"data": {"data": {"apitoken": "x", "other": "y"}}})])
        with _patched(rec) as _, pytest.raises(VaultError) as e:
            await read_secret(_inst(), mount="kvv2", path="a", field="nope", static_token="t")
        assert "apitoken, other" in str(e.value)

    @pytest.mark.asyncio
    async def test_an_empty_mount_or_path_is_rejected_before_any_request(self):
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError, match="mount and a path"):
            await read_secret(_inst(), mount="", path="a", field="k", static_token="t")
        assert rec.seen == [], "a malformed reference must not reach Vault"

    @pytest.mark.asyncio
    async def test_token_auth_without_a_token_is_rejected(self):
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError, match="no token was supplied"):
            await read_secret(_inst(), mount="kvv2", path="a", field="k")


class TestAllowList:
    @pytest.mark.asyncio
    async def test_a_path_outside_the_list_never_reaches_vault(self):
        # The guard is only worth having if it refuses *before* the request —
        # otherwise Vault has already been asked.
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError, match="not in the allow-list"):
            await read_secret(
                _inst(paths=["kvv2/apps"]), mount="kvv2", path="other", field="k", static_token="t"
            )
        assert rec.seen == []

    @pytest.mark.asyncio
    async def test_a_listed_prefix_is_permitted(self):
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            got = await read_secret(
                _inst(paths=["kvv2/apps"]),
                mount="kvv2",
                path="apps/netbox",
                field="k",
                static_token="t",
            )
        assert got == "v"

    @pytest.mark.asyncio
    async def test_an_empty_list_means_unrestricted(self):
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            assert (
                await read_secret(
                    _inst(paths=[]), mount="anything", path="at/all", field="k", static_token="t"
                )
                == "v"
            )


class TestKubernetesAuth:
    @pytest.mark.asyncio
    async def test_it_logs_in_then_reads_and_caches_the_token(self):
        """Two reads, one login: a run with several vault variables must not
        re-authenticate per variable."""
        rec = _Recorder(
            [
                (200, {"auth": {"client_token": "s.tok", "lease_duration": 3600}}),
                (200, {"data": {"data": {"k": "v1"}}}),
                (200, {"data": {"data": {"k": "v2"}}}),
            ]
        )
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "terrapod"})
        with _patched(rec), patch.object(vault_client, "_read_sa_token", return_value="jwt"):
            assert await read_secret(inst, mount="kvv2", path="a", field="k") == "v1"
            assert await read_secret(inst, mount="kvv2", path="a", field="k") == "v2"

        assert len(rec.seen) == 3, "expected one login followed by two reads"
        assert rec.seen[0].url.path == "/v1/auth/kubernetes/login"
        assert b'"role": "terrapod"' in rec.seen[0].content.replace(
            b'"role":"terrapod"', b'"role": "terrapod"'
        )
        assert rec.seen[1].headers["X-Vault-Token"] == "s.tok"

    @pytest.mark.asyncio
    async def test_a_short_lease_is_not_cached(self):
        """A token about to expire must not be reused — a long run would start
        with a dead one."""
        rec = _Recorder(
            [
                (200, {"auth": {"client_token": "s.a", "lease_duration": 5}}),
                (200, {"data": {"data": {"k": "v"}}}),
                (200, {"auth": {"client_token": "s.b", "lease_duration": 5}}),
                (200, {"data": {"data": {"k": "v"}}}),
            ]
        )
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "r"})
        with _patched(rec), patch.object(vault_client, "_read_sa_token", return_value="jwt"):
            await read_secret(inst, mount="kvv2", path="a", field="k")
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert len(rec.seen) == 4, "a 5s lease was cached when it should not have been"

    @pytest.mark.asyncio
    async def test_a_failed_login_says_which_mount_and_role(self):
        rec = _Recorder([(403, {"errors": ["service account not authorized"]})])
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "terrapod"})
        with _patched(rec), patch.object(vault_client, "_read_sa_token", return_value="jwt"):
            with pytest.raises(VaultError, match="mount 'kubernetes', role 'terrapod'"):
                await read_secret(inst, mount="kvv2", path="a", field="k")

    @pytest.mark.asyncio
    async def test_a_missing_sa_token_explains_why(self):
        """Running outside Kubernetes is a configuration error, not a crash."""
        rec = _Recorder([])
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "r"})
        with (
            _patched(rec),
            patch.object(vault_client.Path, "read_text", side_effect=OSError("no such file")),
        ):
            with pytest.raises(VaultError, match="only works when Terrapod runs in-cluster"):
                await read_secret(inst, mount="kvv2", path="a", field="k")


class TestTheAllowListCannotBeWalkedOutOf:
    """The allow-list is documented as refusing reads outside its prefixes
    *whatever the policy permits*. It was bypassable two ways, and the person
    who writes the reference is anyone with write on one workspace."""

    @pytest.mark.parametrize(
        "path",
        [
            "apps/../../../sys/mounts",  # httpx normalises the dots away
            "apps/../secrets/prod",
            "apps/./../../sys/policy/root",
        ],
    )
    @pytest.mark.asyncio
    async def test_dot_segments_are_refused_before_any_request(self, path):
        # httpx resolves `..` when it builds the URL, so a check on the raw
        # string guards a path Vault never sees.
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError):
            await read_secret(
                _inst(paths=["kvv2/apps"]), mount="kvv2", path=path, field="k", static_token="t"
            )
        assert rec.seen == [], "a traversal reference reached Vault"

    @pytest.mark.asyncio
    async def test_dot_segments_are_refused_even_with_no_allow_list(self):
        """Traversal is malformed regardless of whether an allow-list is set —
        otherwise the default configuration is the permissive one."""
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError):
            await read_secret(
                _inst(), mount="kvv2", path="apps/../../sys/mounts", field="k", static_token="t"
            )
        assert rec.seen == []

    @pytest.mark.asyncio
    async def test_a_prefix_must_match_on_a_path_segment(self):
        """`kvv2/apps` must not grant `kvv2/apps-secret` — a bare string prefix
        silently widens the allow-list to sibling paths."""
        rec = _Recorder([])
        for path in ("apps-secret/prod", "appsomething", "apps-admin"):
            with _patched(rec), pytest.raises(VaultError, match="allow-list"):
                await read_secret(
                    _inst(paths=["kvv2/apps"]),
                    mount="kvv2",
                    path=path,
                    field="k",
                    static_token="t",
                )
        assert rec.seen == []

    @pytest.mark.asyncio
    async def test_the_intended_subtree_still_works(self):
        """The refusals above are worthless if they also break the real case."""
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            got = await read_secret(
                _inst(paths=["kvv2/apps"]),
                mount="kvv2",
                path="apps/netbox",
                field="k",
                static_token="t",
            )
        assert got == "v"


class TestATransportFailureIsAVaultError:
    """Only VaultError is caught upstream. A bare httpx error escaped both
    handlers, 500'd the listener, and left the run claimed in `planning` until
    the hour-long stale sweep — so a brief Vault outage stranded every queued
    run in the estate."""

    @pytest.mark.asyncio
    async def test_a_connection_failure_becomes_a_vault_error(self):
        def boom(request):
            raise httpx.ConnectError("connection refused", request=request)

        with _patched(boom), pytest.raises(VaultError, match="unreachable|connect"):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    async def test_a_timeout_becomes_a_vault_error(self):
        def boom(request):
            raise httpx.ReadTimeout("too slow", request=request)

        with _patched(boom), pytest.raises(VaultError):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    async def test_a_non_json_body_becomes_a_vault_error(self):
        """A proxy or ingress returning an HTML error page must not raise a raw
        JSONDecodeError out of the resolver."""

        def html(request):
            return httpx.Response(200, text="<html>502 Bad Gateway</html>")

        with _patched(html), pytest.raises(VaultError):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    async def test_a_login_transport_failure_becomes_a_vault_error(self):
        def boom(request):
            raise httpx.ConnectError("no route to host", request=request)

        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "r"})
        with _patched(boom), patch.object(vault_client, "_read_sa_token", return_value="jwt"):
            with pytest.raises(VaultError):
                await read_secret(inst, mount="kvv2", path="a", field="k")


class TestErrorsDoNotEchoVaultResponseBodies:
    """The message becomes the run's `error_message`, readable by anyone with
    run-read. A third party's response body is not ours to forward there."""

    @pytest.mark.asyncio
    async def test_a_server_error_body_is_not_echoed(self):
        rec = _Recorder([(500, {"errors": ["internal detail nobody should relay"]})])
        with _patched(rec), pytest.raises(VaultError) as e:
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")
        assert "nobody should relay" not in str(e.value)
        assert "500" in str(e.value), "the status is still needed to diagnose it"


class TestTraversalGuardCoversEncodedForms:
    """A raw segment check misses `%2e%2e`, which is decoded downstream — the
    same check-one-path-send-another mismatch by another spelling."""

    @pytest.mark.parametrize(
        ("mount", "path"),
        [
            ("kvv2", "apps/%2e%2e/sys"),  # encoded ..
            ("kvv2", "apps/..%2fsys"),  # encoded separator
            ("kv%2fv2", "apps/x"),  # encoding in the MOUNT half
            ("kvv2", "apps/x#frag"),
            ("kvv2", "apps/x?a=b"),
        ],
    )
    @pytest.mark.asyncio
    async def test_encoded_and_illegal_forms_never_reach_vault(self, mount, path):
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError, match="traversal|illegal"):
            await read_secret(_inst(), mount=mount, path=path, field="k", static_token="t")
        assert rec.seen == [], "a malformed reference reached Vault"

    @pytest.mark.asyncio
    async def test_an_ordinary_path_is_untouched(self):
        """The guard must not reject the paths people actually use."""
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        with _patched(rec):
            assert (
                await read_secret(
                    _inst(), mount="kvv2", path="apps/team-a/netbox_v2", field="k", static_token="t"
                )
                == "v"
            )


class TestASealedVaultIsTransientNotFatal:
    """A sealed Vault answers 503 to everything, and a restarting or unsealing
    one does the same. Before this classifier only TRANSPORT failures were
    transient, so `vault operator seal` errored every queued run in the estate —
    the design worked for a Vault that was unreachable, and not for one that was
    merely shut, which is by far the more common case.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 473, 412])
    async def test_a_transient_status_raises_VaultUnavailable(self, status):
        rec = _Recorder([(status, {})])
        with _patched(rec), pytest.raises(VaultUnavailable):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 403, 405, 422])
    async def test_a_definitive_status_stays_fatal(self, status):
        """A real answer, even a refusal, must NOT be retried forever — that
        would hide a misconfigured reference behind an endless re-queue."""
        rec = _Recorder([(status, {})])
        with _patched(rec), pytest.raises(VaultError) as e:
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")
        assert not isinstance(e.value, VaultUnavailable), (
            f"HTTP {status} is a definitive answer and must not be treated as transient"
        )

    @pytest.mark.asyncio
    async def test_a_sealed_vault_on_LOGIN_is_transient_too(self):
        """Login is what a sealed Vault turns away first, and the transport
        layer does not retry POST — so an unclassified 503 there errored the run
        on the very first attempt."""
        rec = _Recorder([(503, {})])
        inst = _inst(auth={"method": "kubernetes", "mount": "kubernetes", "role": "r"})
        with (
            _patched(rec),
            patch.object(vault_client, "_read_sa_token", return_value="jwt"),
            pytest.raises(VaultUnavailable),
        ):
            await read_secret(inst, mount="kvv2", path="a", field="k")


# ── #1650: jwt auth and a custom CA per instance ────────────────────────────


def _capturing(rec, kwargs_seen: list):
    """Like _patched, but also records the kwargs each AsyncClient was built with
    — the only place the TLS `verify=` decision is observable."""

    def factory(*_a, **kw):
        kwargs_seen.append(kw)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(rec))

    return patch.object(vault_client.httpx, "AsyncClient", factory)


def _write_ca(path, cn: str) -> None:
    """A real self-signed CA certificate, so ssl actually parses and loads it."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _ca_cns(ctx) -> set[str]:
    return {
        value
        for cert in ctx.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }


def _login_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


def _ok_login_then_read() -> _Recorder:
    return _Recorder(
        [
            (200, {"auth": {"client_token": "s.jwt", "lease_duration": 3600}}),
            (200, {"data": {"data": {"k": "v"}}}),
        ]
    )


class TestJwtAuth:
    """The API logs in with a projected ServiceAccount token that Vault checks
    against the cluster's OIDC discovery / JWKS — no TokenReview reach-back."""

    @pytest.mark.asyncio
    async def test_the_login_payload_is_the_token_read_from_token_path(self, tmp_path):
        tok = tmp_path / "token"
        tok.write_text("hdr.payload.sig\n")  # the kubelet's trailing newline is stripped
        rec = _ok_login_then_read()
        inst = _inst(auth={"method": "jwt", "role": "terrapod", "token_path": str(tok)})
        with _patched(rec):
            assert await read_secret(inst, mount="kvv2", path="a", field="k") == "v"

        assert rec.seen[0].method == "POST"
        assert rec.seen[0].url.path == "/v1/auth/jwt/login", "jwt defaults its mount to `jwt`"
        assert _login_body(rec.seen[0]) == {"role": "terrapod", "jwt": "hdr.payload.sig"}
        assert rec.seen[1].headers["X-Vault-Token"] == "s.jwt"

    @pytest.mark.asyncio
    async def test_the_token_file_is_re_read_on_every_login(self, tmp_path):
        """The kubelet rotates a projected token (ten minutes here). A token
        read once and remembered would be expired by the second login."""
        tok = tmp_path / "token"
        tok.write_text("first")
        rec = _Recorder(
            [
                (200, {"auth": {"client_token": "s.a", "lease_duration": 5}}),
                (200, {"data": {"data": {"k": "v"}}}),
                (200, {"auth": {"client_token": "s.b", "lease_duration": 5}}),
                (200, {"data": {"data": {"k": "v"}}}),
            ]
        )
        inst = _inst(auth={"method": "jwt", "token_path": str(tok)})
        with _patched(rec):
            await read_secret(inst, mount="kvv2", path="a", field="k")
            tok.write_text("rotated")
            await read_secret(inst, mount="kvv2", path="a", field="k")

        logins = [r for r in rec.seen if r.url.path.endswith("/login")]
        assert [_login_body(r)["jwt"] for r in logins] == ["first", "rotated"]

    @pytest.mark.asyncio
    async def test_an_explicit_mount_is_used(self, tmp_path):
        tok = tmp_path / "token"
        tok.write_text("t")
        rec = _ok_login_then_read()
        inst = _inst(auth={"method": "jwt", "mount": "k8s-prod", "token_path": str(tok)})
        with _patched(rec):
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert rec.seen[0].url.path == "/v1/auth/k8s-prod/login"

    @pytest.mark.asyncio
    async def test_the_namespace_rides_on_the_login_too(self, tmp_path):
        """HCP Vault Dedicated is jwt auth under `namespace: admin`; the login
        itself must carry the namespace, not just the reads."""
        tok = tmp_path / "token"
        tok.write_text("t")
        rec = _ok_login_then_read()
        inst = _inst(namespace="admin", auth={"method": "jwt", "token_path": str(tok)})
        with _patched(rec):
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert rec.seen[0].headers["X-Vault-Namespace"] == "admin"
        assert rec.seen[1].headers["X-Vault-Namespace"] == "admin"

    @pytest.mark.asyncio
    async def test_a_missing_token_file_is_a_clear_error_not_a_crash(self, tmp_path):
        rec = _Recorder([])
        inst = _inst(auth={"method": "jwt", "token_path": str(tmp_path / "absent")})
        with _patched(rec), pytest.raises(VaultError, match="projected ServiceAccount token"):
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert rec.seen == [], "no login may be attempted without a token"

    @pytest.mark.asyncio
    async def test_an_empty_token_file_is_refused_before_login(self, tmp_path):
        tok = tmp_path / "token"
        tok.write_text("\n")
        rec = _Recorder([])
        inst = _inst(auth={"method": "jwt", "token_path": str(tok)})
        with _patched(rec), pytest.raises(VaultError, match="is empty"):
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert rec.seen == []

    @pytest.mark.asyncio
    async def test_a_refused_login_names_the_audience(self, tmp_path):
        """A mismatched `aud` claim looks like any other refusal from here, so
        the message has to put the audience in front of the operator."""
        tok = tmp_path / "token"
        tok.write_text("t")
        rec = _Recorder([(400, {"errors": ["invalid audience (aud) claim"]})])
        inst = _inst(auth={"method": "jwt", "role": "terrapod", "token_path": str(tok)})
        with _patched(rec), pytest.raises(VaultError, match="audience 'vault'") as e:
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert "invalid audience" not in str(e.value), "Vault's body is never echoed"
        assert not isinstance(e.value, VaultUnavailable)

    @pytest.mark.asyncio
    async def test_kubernetes_auth_honours_a_configured_token_path(self, tmp_path):
        tok = tmp_path / "aud-token"
        tok.write_text("projected")
        rec = _ok_login_then_read()
        inst = _inst(auth={"method": "kubernetes", "audience": "vault", "token_path": str(tok)})
        with _patched(rec):
            await read_secret(inst, mount="kvv2", path="a", field="k")
        assert rec.seen[0].url.path == "/v1/auth/kubernetes/login"
        assert _login_body(rec.seen[0])["jwt"] == "projected"


class TestTlsVerification:
    """The `verify=` every client is built with, per instance."""

    @pytest.mark.asyncio
    async def test_no_ca_file_passes_verify_true(self):
        """True, not a context: that is the value httpx turns into "honour
        SSL_CERT_FILE" — which is how the chart's global caBundle reaches Vault."""
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        seen: list = []
        with _capturing(rec, seen):
            await read_secret(_inst(), mount="kvv2", path="a", field="k", static_token="t")
        assert [kw["verify"] for kw in seen] == [True]

    @pytest.mark.asyncio
    async def test_skip_verify_still_passes_false(self):
        rec = _Recorder([(200, {"data": {"data": {"k": "v"}}})])
        seen: list = []
        with _capturing(rec, seen):
            await read_secret(
                _inst(tls_skip_verify=True), mount="kvv2", path="a", field="k", static_token="t"
            )
        assert [kw["verify"] for kw in seen] == [False]

    @pytest.mark.asyncio
    async def test_a_ca_file_becomes_an_ssl_context_trusting_only_it(self, tmp_path):
        ca = tmp_path / "ca.crt"
        _write_ca(ca, "Example Vault CA")
        tok = tmp_path / "token"
        tok.write_text("t")
        rec = _ok_login_then_read()
        seen: list = []
        inst = _inst(ca_file=str(ca), auth={"method": "jwt", "token_path": str(tok)})
        with _capturing(rec, seen):
            await read_secret(inst, mount="kvv2", path="a", field="k")

        assert len(seen) == 2, "the login and the read each build a client"
        for kw in seen:
            ctx = kw["verify"]
            assert isinstance(ctx, ssl.SSLContext), "login and read must both verify"
            assert ctx.verify_mode == ssl.CERT_REQUIRED
            assert ctx.check_hostname is True
            # Pinned: ONLY this CA, not the default roots alongside it.
            assert _ca_cns(ctx) == {"Example Vault CA"}

    @pytest.mark.asyncio
    async def test_a_rotated_ca_file_is_picked_up(self, tmp_path):
        """The kubelet remounts a changed Secret; the cache must not pin the old CA."""
        ca = tmp_path / "ca.crt"
        _write_ca(ca, "Old CA")
        inst = _inst(ca_file=str(ca))
        first = await vault_client._verify_for(inst)
        assert await vault_client._verify_for(inst) is first, "an unchanged file is cached"

        _write_ca(ca, "New CA")
        st = os.stat(ca)
        os.utime(ca, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        second = await vault_client._verify_for(inst)
        assert _ca_cns(second) == {"New CA"}

    @pytest.mark.asyncio
    async def test_a_missing_ca_file_fails_before_any_request(self, tmp_path):
        rec = _Recorder([])
        inst = _inst(ca_file=str(tmp_path / "absent.crt"))
        with _patched(rec), pytest.raises(VaultError, match="could not read the CA file"):
            await read_secret(inst, mount="kvv2", path="a", field="k", static_token="t")
        assert rec.seen == []

    @pytest.mark.asyncio
    async def test_a_garbage_ca_file_is_a_clear_error(self, tmp_path):
        ca = tmp_path / "ca.crt"
        ca.write_text("-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----\n")
        rec = _Recorder([])
        with _patched(rec), pytest.raises(VaultError, match="not a usable PEM"):
            await read_secret(
                _inst(ca_file=str(ca)), mount="kvv2", path="a", field="k", static_token="t"
            )
        assert rec.seen == []

    def test_pinned_httpx_honours_ssl_cert_file_for_verify_true(self, tmp_path, monkeypatch):
        """The documented answer in docs/vault.md, pinned against the httpx the
        image resolves: `verify=True` + SSL_CERT_FILE loads that bundle. If an
        httpx upgrade stops doing so, the global caBundle silently stops
        covering Vault, and this fails first."""
        from httpx._config import create_ssl_context

        ca = tmp_path / "bundle.crt"
        _write_ca(ca, "Global Bundle CA")
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        assert "Global Bundle CA" in _ca_cns(create_ssl_context(verify=True))
