"""Vault client unit tests (#1439).

Driven through `httpx.MockTransport` so the real request/response handling runs
— URL construction, the kv-v2 `data.data` nesting, headers, status handling —
rather than patching the client and asserting on a mock.

The live proof against a real Vault in-cluster is separate and does not replace
these: it cannot exercise every error branch, and it does not run in CI.
"""

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
def _clear_cache():
    reset_token_cache()
    yield
    reset_token_cache()


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
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 473])
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
