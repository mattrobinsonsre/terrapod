"""What the runner tells the Pulumi CLI to do (#1523).

The module had no tests. Everything here is a thing that fails *silently* when it
is wrong — the CLI carries on and does something plausible with a default — which
is why they are worth pinning rather than left to the live smoke:

  * a plugin-override pattern that matches nothing falls back to get.pulumi.com
  * a preview and its update disagreeing about the plan file surfaces as "no
    plan file" on the update, a long way from the preview that should have
    written it

The backend itself is pinned next door in `test_pulumi_service_backend.py`:
since #1881 an agent run speaks to Terrapod's Pulumi service surface, and what
needs guarding there is the *absence* of the file-backend scaffolding rather
than the argv this file is about.
"""

from __future__ import annotations

import dataclasses

import pytest

from terrapod.runner.phases import pulumi_exec
from terrapod.runner.runner_config import RunnerConfig

API = "https://terrapod.test"


def _cfg(**over) -> RunnerConfig:
    cfg = RunnerConfig.from_env(
        env={
            "TP_API_URL": API,
            "TP_AUTH_TOKEN": "tok",
            "TP_RUN_ID": "run-1",
            "TP_BACKEND": "tofu",
            "TP_VERSION": "1.12.1",
        }
    )
    return dataclasses.replace(cfg, **({"os": "linux", "arch": "amd64", **over}))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The argv builders read TP_* directly, so leakage between tests is real."""
    for k in (
        "TP_PULUMI_STACK",
        "TP_REFRESH",
        "TP_PARALLELISM",
        "TP_TARGET_URNS",
        "TP_DESTROY",
    ):
        monkeypatch.delenv(k, raising=False)


class TestThePluginOverride:
    def test_the_pattern_is_the_catch_all(self) -> None:
        """The one that cannot miss.

        An anchored pattern that fails to match does not error — the CLI just
        uses its default host. So it works for anyone with egress and hangs for
        anyone air-gapped, which is the failure this asserts away.
        """
        value = pulumi_exec.plugin_override_env(API, "tok", 9999)[
            "PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"
        ]
        assert value.startswith(".*=")

    def test_it_points_at_the_loopback_shim_not_the_api(self) -> None:
        """#1906. Straight at the API, every download answered 401.

        The cache requires a credential and Pulumi's plugin downloader sends
        none — `PULUMI_ACCESS_TOKEN` is the service backend's and is not carried
        to a plugin host — so no program using any provider could run. The shim
        holds the token; the CLI carries nothing and has nothing to refuse.
        """
        value = pulumi_exec.plugin_override_env(API, "tok", 5432)[
            "PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"
        ]
        assert value == ".*=http://127.0.0.1:5432"
        assert API not in value, "the CLI must not be pointed at a host it cannot authenticate to"

    def test_the_token_is_carried(self) -> None:
        assert pulumi_exec.plugin_override_env(API, "tok", 1)["PULUMI_ACCESS_TOKEN"] == "tok"

    def test_no_api_url_sets_nothing(self) -> None:
        assert pulumi_exec.plugin_override_env("", "tok", 1) == {}

    def test_the_port_is_required(self) -> None:
        """Not defaulted, because the only default is the broken direct URL —
        a caller that forgot would silently get back the 401."""
        import inspect

        params = inspect.signature(pulumi_exec.plugin_override_env).parameters
        assert params["proxy_port"].default is inspect.Parameter.empty


class TestThePluginProxyAuthenticates:
    """The behaviour, not the spelling of the env var.

    Every test above this asserted the shape of a string. That is exactly what
    let #1906 ship: the override was well-formed, agreed with the backend on its
    prefix and carried a token nobody sent — and no provider could be downloaded.
    """

    SECRET = "runtok:abc"  # noqa: S105 - a fixture, not a credential

    def test_a_plugin_download_reaches_the_cache_with_the_runs_token(self) -> None:
        import urllib.request
        from unittest.mock import patch

        seen: dict[str, object] = {}

        class _Resp:
            status_code = 200
            # A real httpx response always has these; the shim reads them
            # to decide what to forward.
            headers: dict[str, str] = {}

            def iter_bytes(self):
                yield b"plugin-tarball"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_stream(method, url, headers=None, timeout=None, follow_redirects=False):
            seen["url"] = url
            seen["auth"] = (headers or {}).get("Authorization")
            return _Resp()

        with patch.object(pulumi_exec.httpx, "stream", fake_stream):
            proxy = pulumi_exec.CacheProxy(API, self.SECRET, "pulumi")
            proxy.start()
            try:
                env = pulumi_exec.plugin_override_env(API, self.SECRET, proxy.port)
                base = env["PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"].removeprefix(".*=")
                got = urllib.request.urlopen(
                    f"{base}/pulumi-resource-random-v4.21.2-linux-arm64.tar.gz", timeout=10
                ).read()
            finally:
                proxy.stop()

        assert got == b"plugin-tarball"
        assert seen["auth"] == f"Bearer {self.SECRET}", (
            "the shim did not carry the run's token — this is the 401 the CLI got"
        )
        assert seen["url"] == (
            f"{API}/api/terrapod/v1/package-cache/pulumi"
            "/pulumi-resource-random-v4.21.2-linux-arm64.tar.gz"
        )

    def test_the_body_length_is_forwarded(self) -> None:
        """Pulumi refuses a download whose length it cannot confirm.

        It compares what it copied against Content-Length, and an absent header
        reads as -1, so it never matches: *"expected -1 bytes but copied
        19525050"*. The plugin arrived intact and was thrown away. The go
        command does not check, which is how the shim ran without this.
        """
        import urllib.request
        from unittest.mock import patch

        body = b"x" * 4096

        class _Resp:
            status_code = 200
            headers = {"content-length": str(len(body)), "content-type": "application/gzip"}

            def iter_bytes(self):
                yield body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(pulumi_exec.httpx, "stream", lambda *a, **k: _Resp()):
            proxy = pulumi_exec.CacheProxy(API, "t", "pulumi")
            proxy.start()
            try:
                resp = urllib.request.urlopen(f"http://127.0.0.1:{proxy.port}/p.tar.gz", timeout=10)
                got = resp.read()
            finally:
                proxy.stop()

        assert resp.headers["Content-Length"] == str(len(body))
        assert resp.headers["Content-Type"] == "application/gzip"
        assert got == body

    def test_an_encoded_body_forwards_no_length(self) -> None:
        """httpx decompresses on the way through, so the upstream's length
        describes bytes the shim no longer has. Sending it would swap one
        mismatch for another."""
        import urllib.request
        from unittest.mock import patch

        class _Resp:
            status_code = 200
            headers = {"content-length": "11", "content-encoding": "gzip"}

            def iter_bytes(self):
                yield b"decompressed-and-longer"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(pulumi_exec.httpx, "stream", lambda *a, **k: _Resp()):
            proxy = pulumi_exec.CacheProxy(API, "t", "pulumi")
            proxy.start()
            try:
                resp = urllib.request.urlopen(f"http://127.0.0.1:{proxy.port}/p", timeout=10)
                got = resp.read()
            finally:
                proxy.stop()

        assert resp.headers.get("Content-Length") is None
        assert got == b"decompressed-and-longer"

    def test_the_two_segments_do_not_share_an_upstream(self) -> None:
        """One class, two caches. A segment that leaked would send plugin
        requests to the Go proxy, which answers 404 for every one of them."""
        pulumi = pulumi_exec.CacheProxy(API, "t", "pulumi")
        go = pulumi_exec.CacheProxy(API, "t", "go")
        try:
            assert pulumi._upstream.endswith("/package-cache/pulumi")
            assert go._upstream.endswith("/package-cache/go")
        finally:
            pulumi.stop()
            go.stop()


class TestThePhaseArgv:
    def test_preview_saves_the_plan_the_update_reads(self) -> None:
        """The pairing is what makes an approved preview and its update one
        decision rather than two independent runs."""
        plan = "/workspace/plan.json"
        assert f"--save-plan={plan}" in pulumi_exec.preview_argv(plan, _cfg())
        assert f"--plan={plan}" in pulumi_exec.update_argv(plan, _cfg())

    def test_both_phases_are_non_interactive(self) -> None:
        """A runner Job has no terminal; a prompt is a hang until the timeout."""
        assert "--non-interactive" in pulumi_exec.preview_argv("p", _cfg())
        assert "--non-interactive" in pulumi_exec.update_argv("p", _cfg())

    def test_the_update_confirms_itself(self) -> None:
        assert "--yes" in pulumi_exec.update_argv("p", _cfg())

    def test_an_absent_plan_degrades_to_an_unconstrained_up(self) -> None:
        """The preview runs in a different pod from the update, so the saved plan
        is an artifact that has to survive the hop. When it does not, `up` still
        applies the same configuration — it is simply no longer constrained to
        the operations the preview showed.

        Refusing instead would strand a run whose preview had just succeeded,
        which is the behaviour the Terraform path deliberately avoids via
        `has_plan_file`.
        """
        argv = pulumi_exec.update_argv("", _cfg())
        assert argv[0] == "up"
        assert "--yes" in argv
        assert not any(a.startswith("--plan=") for a in argv), (
            "an empty plan path must drop the flag, not pass `--plan=` with no value"
        )

    def test_a_destroy_takes_no_plan(self, monkeypatch) -> None:
        """`destroy` rejects a plan file — there is nothing to preview into one
        that it would read back."""
        monkeypatch.setenv("TP_DESTROY", "true")
        argv = pulumi_exec.update_argv("/workspace/plan.json", _cfg())
        assert argv[0] == "destroy"
        assert not any(a.startswith("--plan=") for a in argv)

    def test_the_stack_is_passed_as_terrapod_names_it(self, monkeypatch) -> None:
        """The API sends `default/<project>/<stack>` and that is what the CLI is
        given (#1881).

        It used to be rewritten to `organization/proj/dev` on the way, because a
        file backend accepts a qualified name only under the literal
        organization `organization`. Against Terrapod the first segment is the
        organization, so a rewrite here would name a stack that does not exist.
        """
        monkeypatch.setenv("TP_PULUMI_STACK", "default/proj/dev")
        argv = pulumi_exec.preview_argv("p", _cfg())
        assert argv[argv.index("--stack") + 1] == "default/proj/dev"

    def test_refresh_is_only_disabled_when_asked(self, monkeypatch) -> None:
        assert "--refresh=false" not in pulumi_exec.preview_argv("p", _cfg())
        monkeypatch.setenv("TP_REFRESH", "false")
        assert "--refresh=false" in pulumi_exec.preview_argv("p", _cfg())

    def test_targets_become_repeated_flags(self, monkeypatch) -> None:
        monkeypatch.setenv("TP_TARGET_URNS", '["urn:a", "urn:b"]')
        argv = pulumi_exec.preview_argv("p", _cfg())
        assert argv.count("--target") == 2
        assert "urn:a" in argv and "urn:b" in argv

    def test_an_empty_target_list_adds_nothing(self, monkeypatch) -> None:
        """`--target` with no value would scope the run to nothing at all."""
        monkeypatch.setenv("TP_TARGET_URNS", "")
        assert "--target" not in pulumi_exec.preview_argv("p", _cfg())
