"""Tests for terrapod.runner.phases.uploads.

Each helper is a thin httpx wrapper. We test:
  * Happy-path: the right URL is called with the right method + body.
  * No-API short-circuit (degenerate dev invocations with TP_API_URL
    unset).
  * Missing/empty file: skip without raising.
  * Non-2xx response: function returns False (best-effort) or signals
    the appropriate side-effect (state-diverged for state).
"""

from __future__ import annotations

import httpx

from terrapod.runner.phases import uploads
from terrapod.runner.runner_config import RunnerConfig


def _cfg(**overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── upload_lock_file ──────────────────────────────────────────────────


class TestUploadLockFile:
    def test_happy_path(self, tmp_path) -> None:
        lock = tmp_path / ".terraform.lock.hcl"
        lock.write_text("provider {}\n")
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content))
            return httpx.Response(204)

        ok = uploads.upload_lock_file(_cfg(), lock, client=_client(handler))
        assert ok
        assert len(seen) == 1
        method, url, body = seen[0]
        assert method == "PUT"
        assert url.endswith("/api/terrapod/v1/runs/run-1/artifacts/lock-file")
        assert body == b"provider {}\n"

    def test_missing_file_short_circuits(self, tmp_path) -> None:
        ok = uploads.upload_lock_file(_cfg(), tmp_path / "absent.hcl")
        assert ok is False

    def test_no_api_returns_false(self, tmp_path) -> None:
        lock = tmp_path / "lock.hcl"
        lock.write_text("x\n")
        ok = uploads.upload_lock_file(_cfg(TP_API_URL="", TP_RUN_ID=""), lock)
        assert ok is False

    def test_server_error_returns_false_does_not_raise(self, tmp_path) -> None:
        lock = tmp_path / ".terraform.lock.hcl"
        lock.write_text("x\n")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"server error")

        ok = uploads.upload_lock_file(_cfg(), lock, client=_client(handler))
        assert ok is False


# ── upload_plan_file / upload_plan_json ───────────────────────────────


class TestUploadPlanArtifacts:
    def test_plan_file_octet_stream(self, tmp_path) -> None:
        plan = tmp_path / "tfplan"
        plan.write_bytes(b"\x00\x01binary plan data")
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200)

        ok = uploads.upload_plan_file(_cfg(), plan, client=_client(handler))
        assert ok
        assert seen_headers.get("content-type") == "application/octet-stream"

    def test_plan_json_content_type_application_json(self, tmp_path) -> None:
        pjson = tmp_path / "plan.json"
        pjson.write_text('{"resource_changes":[]}')
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200)

        ok = uploads.upload_plan_json(_cfg(), pjson, client=_client(handler))
        assert ok
        assert seen_headers.get("content-type") == "application/json"


# ── upload_state + signal_state_diverged ──────────────────────────────


class TestUploadState:
    def test_happy_path(self, tmp_path) -> None:
        state = tmp_path / "terraform.tfstate"
        state.write_text('{"version":4}')

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        ok = uploads.upload_state(_cfg(), state, client=_client(handler))
        assert ok

    def test_failed_state_upload_returns_false(self, tmp_path) -> None:
        state = tmp_path / "terraform.tfstate"
        state.write_text("{}")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        ok = uploads.upload_state(_cfg(), state, client=_client(handler))
        assert ok is False


class TestSignalStateDiverged:
    def test_posts_to_state_diverged_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(204)

        ok = uploads.signal_state_diverged(_cfg(), client=_client(handler))
        assert ok
        assert any(u.endswith("/runs/run-1/state-diverged") for u in seen)


# ── plan-result / apply-result ────────────────────────────────────────


class TestRunResults:
    def test_plan_result_includes_has_changes_body(self) -> None:
        seen_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            seen_bodies.append(_json.loads(request.content))
            return httpx.Response(204)

        client = _client(handler)
        ok = uploads.post_plan_result(_cfg(), has_changes=True, client=client)
        assert ok
        assert seen_bodies == [{"has_changes": True}]

    def test_apply_result_no_body(self) -> None:
        seen_bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_bodies.append(request.content)
            return httpx.Response(204)

        ok = uploads.post_apply_result(_cfg(), client=_client(handler))
        assert ok
        # Empty body — apply-result is a side-effect-only POST.
        assert seen_bodies[0] in (b"", b"null", b"{}")


# ── CLI entrypoint ────────────────────────────────────────────────────


class TestCliMain:
    def test_state_failure_returns_1(self, tmp_path, monkeypatch) -> None:
        """The only subcommand that exits non-zero on upload failure is
        `state` — the rest are best-effort and exit 0 regardless."""
        state = tmp_path / "state.json"
        state.write_text("{}")

        monkeypatch.setenv("TP_API_URL", "https://api.example.com")
        monkeypatch.setenv("TP_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TP_RUN_ID", "run-1")

        # Inject a MockTransport into every httpx.Client the CLI builds
        # by patching the constructor. Capture the REAL Client first to
        # avoid recursion when our replacement calls back into Client.
        real_client = httpx.Client

        def _bad_client(*args, **kwargs):
            kwargs.pop("transport", None)
            return real_client(
                transport=httpx.MockTransport(lambda req: httpx.Response(500)),
                **kwargs,
            )

        monkeypatch.setattr(httpx, "Client", _bad_client)

        rc = uploads._cli_main(argv=["state", str(state)])
        assert rc == 1

    def test_plan_result_returns_0_even_on_failure(self, monkeypatch) -> None:
        monkeypatch.setenv("TP_API_URL", "https://api.example.com")
        monkeypatch.setenv("TP_AUTH_TOKEN", "tok")
        monkeypatch.setenv("TP_RUN_ID", "run-1")

        real_client = httpx.Client

        def _bad_client(*args, **kwargs):
            kwargs.pop("transport", None)
            return real_client(
                transport=httpx.MockTransport(lambda req: httpx.Response(500)),
                **kwargs,
            )

        monkeypatch.setattr(httpx, "Client", _bad_client)

        rc = uploads._cli_main(argv=["plan-result", "--has-changes", "true"])
        assert rc == 0  # best-effort

    def test_help_flag_works(self) -> None:
        """Sanity check the argparse skeleton."""
        import pytest as _pt

        with _pt.raises(SystemExit) as ei:
            uploads._cli_main(argv=["--help"])
        assert ei.value.code == 0
