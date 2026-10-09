"""API-side OPA acquisition and its degradation contract (#1208).

The point of these tests is the asymmetry with the runner: there, a missing OPA
must stop the run; here it must not stop an operator editing a policy. What is
*not* negotiable on either side is the artifact — both sides fetch through the
binary cache, which is the one place that decides a binary is trustworthy.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import shutil
import stat
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import api_opa, policy_engine


@contextlib.contextmanager
def _cache_serving(storage, *, cached: AsyncMock | None = None):
    """Stand in for the binary cache and the object store behind it.

    `_download` imports `get_db_session`, `get_or_cache_binary` and
    `get_storage` lazily inside the function, so they resolve from their own
    modules at call time -- patching them on `api_opa` would patch nothing and
    the test would silently exercise the real cache. Patch them at the source.
    """

    @contextlib.asynccontextmanager
    async def _session():
        yield AsyncMock()

    with (
        patch("terrapod.db.session.get_db_session", _session),
        patch(
            "terrapod.services.binary_cache_service.get_or_cache_binary",
            cached if cached is not None else AsyncMock(return_value=""),
        ),
        patch("terrapod.storage.get_storage", lambda: storage),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_memo():
    api_opa._reset_for_tests()
    yield
    api_opa._reset_for_tests()


class TestOpaBinary:
    async def test_returns_none_when_the_download_fails(self):
        """None is a normal outcome the caller handles, not an exception."""
        with patch.object(api_opa, "_download", AsyncMock(side_effect=RuntimeError("no route"))):
            assert await api_opa.opa_binary() is None

    async def test_reuses_an_already_downloaded_binary(self, tmp_path, monkeypatch):
        """A restart that finds the PVC warm must not re-download."""
        monkeypatch.setattr(api_opa, "_tool_dir", lambda: tmp_path)
        version = api_opa.configured_version("opa")
        (tmp_path / f"opa-{version}").write_text("#!/opa")
        download = AsyncMock()
        with patch.object(api_opa, "_download", download):
            path = await api_opa.opa_binary()
        assert path == str(tmp_path / f"opa-{version}")
        download.assert_not_awaited()

    async def test_downloads_once_under_concurrent_first_use(self, tmp_path, monkeypatch):
        """Two policy writes arriving together must not both fetch ~50MB."""
        monkeypatch.setattr(api_opa, "_tool_dir", lambda: tmp_path)
        version = api_opa.configured_version("opa")

        calls = []

        async def fake_download(v, dest):
            calls.append(v)
            await asyncio.sleep(0)
            dest.write_text("#!/opa")

        with patch.object(api_opa, "_download", fake_download):
            results = await asyncio.gather(*(api_opa.opa_binary() for _ in range(5)))

        assert calls == [version]
        assert set(results) == {str(tmp_path / f"opa-{version}")}


class TestASealedNodeDoesNotFetchOPA:
    """GHSA-rfxh-5gwg-px2g.

    `registry.cache_only` is documented as "a hard guarantee that the caches never
    reach upstream" (docs/deployment-network-isolation.md), and every other cache
    honours it — this module was the one that did not, so a policy-set write on an
    air-gapped deployment made an outbound request.

    `_tool_dir` is redirected in all of these: the real tool dir may hold an OPA
    from earlier work, in which case `dest.exists()` short-circuits first and the
    sealed branch is never reached. That is exactly how this file's neighbouring
    download test comes to fail on a developer machine and pass in the container.
    """

    async def test_it_does_not_download(self, tmp_path, monkeypatch):
        monkeypatch.setattr(api_opa, "_tool_dir", lambda: tmp_path)
        monkeypatch.setattr(api_opa.settings.registry, "cache_only", True)
        download = AsyncMock()
        with patch.object(api_opa, "_download", download):
            assert await api_opa.opa_binary() is None
        download.assert_not_awaited()

    async def test_a_binary_already_on_the_pvc_is_still_used(self, tmp_path, monkeypatch):
        """Sealing stops the fetch, not the cache — the whole point of a sealed
        node is that it answers from what it already holds."""
        monkeypatch.setattr(api_opa, "_tool_dir", lambda: tmp_path)
        monkeypatch.setattr(api_opa.settings.registry, "cache_only", True)
        version = api_opa.configured_version("opa")
        (tmp_path / f"opa-{version}").write_text("#!/opa")
        download = AsyncMock()
        with patch.object(api_opa, "_download", download):
            assert await api_opa.opa_binary() == str(tmp_path / f"opa-{version}")
        download.assert_not_awaited()

    async def test_an_unsealed_node_still_fetches(self, tmp_path, monkeypatch):
        """The negative path: this must not have turned the fetch off for everyone."""
        monkeypatch.setattr(api_opa, "_tool_dir", lambda: tmp_path)
        monkeypatch.setattr(api_opa.settings.registry, "cache_only", False)
        version = api_opa.configured_version("opa")

        async def _fake(v, dest):
            dest.write_text("#!/opa")

        with patch.object(api_opa, "_download", AsyncMock(side_effect=_fake)) as download:
            assert await api_opa.opa_binary() == str(tmp_path / f"opa-{version}")
        download.assert_awaited_once()


class TestCheckRegoDegrades:
    async def test_reports_unavailable_rather_than_a_compile_error(self):
        """The distinction matters: the caller accepts one and rejects the
        other, so conflating them would reject every policy write whenever an
        unrelated fetch failed."""
        with patch.object(api_opa, "opa_binary", AsyncMock(return_value=None)):
            assert await policy_engine.check_rego("package terrapod") == (
                policy_engine.VALIDATION_UNAVAILABLE
            )

    async def test_uses_the_fetched_binary_when_available(self):
        with patch.object(api_opa, "opa_binary", AsyncMock(return_value="/does/not/exist/opa")):
            result = await policy_engine.check_rego("package terrapod")
        # The path doesn't exist, so exec fails — which routes through the same
        # unavailable signal rather than being reported as broken Rego.
        assert result == policy_engine.VALIDATION_UNAVAILABLE

    @pytest.mark.skipif(shutil.which("opa") is None, reason="opa binary not on PATH")
    async def test_still_reports_genuinely_broken_rego(self):
        """Degrading must not swallow the thing this check exists for.

        **Supplies the binary, like both siblings above.** Omitting it sent this
        test through the acquisition path, so it needed a *working* fetch to
        prove a point about compile errors -- and in a tier whose database is
        mocked, "working" meant reaching upstream over the network. Once the
        fetch went through the cache that stopped resolving, and the assertion
        failed claiming the error was the unavailable signal, which is exactly
        the conflation this class exists to rule out. A test that needs a real
        OPA should say so and skip without one.
        """
        err = await policy_engine.check_rego(
            "package terrapod\n\nthis is not rego {{{", opa_binary="opa"
        )
        assert err is not None
        assert err != policy_engine.VALIDATION_UNAVAILABLE


class TestConcurrentDownloadsDoNotClobber:
    """Two fetches racing into the same destination must both succeed.

    `_download` used a fixed `<dest>.partial` scratch path. The in-process
    `asyncio.Lock` around `opa_binary` hides that from a single process, but it
    does nothing across processes — two pytest-xdist workers, or two API
    replicas sharing the ephemeral PVC. Both stream into the same file, the
    first `replace(dest)` renames it away, and the second dies on `stat()` with
    ENOENT: "OPA binary not available" reported by a fetch that in fact
    succeeded.

    This reproduces the interleaving directly, bypassing the lock by calling
    `_download` rather than `opa_binary`.
    """

    async def test_two_concurrent_downloads_both_succeed(self, tmp_path):
        payload = b"#!/bin/sh\necho opa\n"

        class _FakeStorage:
            async def get_stream(self, _key):
                # Yield in two parts with a suspension between, so the two
                # coroutines are guaranteed to interleave mid-write.
                yield payload[:5]
                await asyncio.sleep(0)
                yield payload[5:]

        dest = tmp_path / "opa-1.19.0"
        with _cache_serving(_FakeStorage()):
            await asyncio.gather(
                api_opa._download("1.19.0", dest),
                api_opa._download("1.19.0", dest),
            )

        assert dest.exists()
        assert dest.read_bytes() == payload
        # And no scratch files left behind.
        assert [p.name for p in tmp_path.iterdir()] == [dest.name]


class TestTheApiFetchesOpaThroughTheCacheOnly:
    """The acquisition path is the binary cache, and nothing else.

    A direct upstream fetch here is not a style preference. It breaks a sealed
    deployment (`registry.cache_only`), it re-downloads on every pod and every
    restart instead of being served from object storage, and it puts a second
    checksum gate beside the cache's own -- two places deciding whether an OPA
    binary is trustworthy, which is one too many. The cache is the single path
    allowed to reach upstream, and `_download` drives it in-process rather than
    over the API's own HTTP surface, so there is no loopback request and no
    credential the API would have to mint for itself.
    """

    async def test_it_populates_the_cache_then_reads_that_object(self, tmp_path):
        from terrapod.storage.keys import binary_cache_key

        read_keys: list[str] = []

        class _FakeStorage:
            async def get_stream(self, key):
                read_keys.append(key)
                yield b"opa-bytes"

        cached = AsyncMock(return_value="https://presigned.invalid/opa")
        dest = tmp_path / "opa-1.19.0"
        with _cache_serving(_FakeStorage(), cached=cached):
            await api_opa._download("1.19.0", dest)

        # The cache was asked for it -- which populates it on a miss and
        # touches `last_accessed_at` on a hit, so retention cannot reap a
        # binary this pod depends on.
        assert cached.await_count == 1
        args = cached.await_args.args
        assert args[2:5] == ("opa", "1.19.0", "linux")
        arch = args[5]
        assert arch in ("amd64", "arm64")

        # And the bytes came back from that same cached object, by key --
        # not from upstream, and not over a presigned URL the API would have
        # to fetch over HTTP.
        assert read_keys == [binary_cache_key("opa", "1.19.0", "linux", arch)]
        assert dest.read_bytes() == b"opa-bytes"
        assert dest.stat().st_mode & stat.S_IXUSR

    def test_no_http_client_is_reachable_from_the_module(self):
        """Asserted on the imports, not the text.

        An AST walk catches a client imported lazily inside a function body --
        which is how the cache and storage are reached here, so it is the shape
        a reinstated upstream fetch would most plausibly take. It also ignores
        the comments that *mention* the old path, which a grep would not.
        """
        tree = ast.parse(inspect.getsource(api_opa))
        reachable: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                reachable.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    reachable.add(node.module.split(".")[0])
                reachable.update(a.name for a in node.names)

        forbidden = {
            "httpx",
            "requests",
            "urllib",
            "urllib3",
            "aiohttp",
            "download_url",
            "verify_platform_tool",
        }
        offenders = sorted(reachable & forbidden)
        assert not offenders, (
            f"{offenders} is reachable from api_opa. The API's OPA fetch goes through "
            "get_or_cache_binary, never upstream directly -- see this class's docstring."
        )
