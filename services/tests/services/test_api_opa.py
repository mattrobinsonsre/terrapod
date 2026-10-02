"""API-side OPA acquisition and its degradation contract (#1208).

The point of these tests is the asymmetry with the runner: there, a missing OPA
must stop the run; here it must not stop an operator editing a policy. What is
*not* negotiable on either side is the artifact — an unverifiable download is
rejected rather than executed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import api_opa, policy_engine


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
        import asyncio

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

    async def test_still_reports_genuinely_broken_rego(self):
        """Degrading must not swallow the thing this check exists for."""
        err = await policy_engine.check_rego("package terrapod\n\nthis is not rego {{{")
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
        import asyncio

        payload = b"#!/bin/sh\necho opa\n"

        class _FakeStream:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aiter_bytes(self, _size):
                # Yield in two parts with a suspension between, so the two
                # coroutines are guaranteed to interleave mid-write.
                yield payload[:5]
                await asyncio.sleep(0)
                yield payload[5:]

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, _method, _url):
                return _FakeStream()

        dest = tmp_path / "opa-1.19.0"
        with (
            patch.object(api_opa.httpx, "AsyncClient", lambda **kw: _FakeClient()),
            patch.object(api_opa, "download_url", lambda *a: "https://example.invalid/opa"),
            patch.object(api_opa, "verify_platform_tool", AsyncMock()),
        ):
            await asyncio.gather(
                api_opa._download("1.19.0", dest),
                api_opa._download("1.19.0", dest),
            )

        assert dest.exists()
        assert dest.read_bytes() == payload
        # And no scratch files left behind.
        assert [p.name for p in tmp_path.iterdir()] == [dest.name]
