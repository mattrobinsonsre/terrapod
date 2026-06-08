"""Tests for terrapod.runner.phases.opa."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from terrapod.runner.phases import opa
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


class TestCoerceToStringList:
    def test_none_returns_empty(self) -> None:
        assert opa._coerce_to_string_list(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert opa._coerce_to_string_list([]) == []

    def test_list_of_strings_sorted(self) -> None:
        assert opa._coerce_to_string_list(["b", "a", "c"]) == ["a", "b", "c"]

    def test_set_sorted(self) -> None:
        assert opa._coerce_to_string_list({"z", "a"}) == ["a", "z"]

    def test_scalar_wrapped(self) -> None:
        assert opa._coerce_to_string_list("oops") == ["oops"]

    def test_bool_wrapped(self) -> None:
        # A misauthored `deny := true` — must surface, not silently pass.
        assert opa._coerce_to_string_list(True) == ["True"]


class TestExtractDenyWarn:
    def test_well_formed(self) -> None:
        payload = json.dumps(
            {
                "result": [
                    {
                        "expressions": [
                            {
                                "value": {"deny": ["x"], "warn": ["y", "z"]},
                            }
                        ],
                    }
                ],
            }
        )
        assert opa._extract_deny_warn(payload) == (["x"], ["y", "z"])

    def test_missing_deny(self) -> None:
        payload = json.dumps(
            {
                "result": [{"expressions": [{"value": {"warn": ["w"]}}]}],
            }
        )
        assert opa._extract_deny_warn(payload) == ([], ["w"])

    def test_empty_result(self) -> None:
        assert opa._extract_deny_warn('{"result": []}') == ([], [])

    def test_no_result_key(self) -> None:
        assert opa._extract_deny_warn("{}") == ([], [])

    def test_garbage_json(self) -> None:
        assert opa._extract_deny_warn("not-json") == ([], [])

    def test_scalar_deny_coerced(self) -> None:
        payload = json.dumps(
            {
                "result": [{"expressions": [{"value": {"deny": "single message"}}]}],
            }
        )
        assert opa._extract_deny_warn(payload) == (["single message"], [])


class TestFetchPolicyBundle:
    def test_no_api_returns_empty_bundle(self) -> None:
        cfg = _cfg(TP_API_URL="", TP_RUN_ID="")
        assert opa.fetch_policy_bundle(cfg) == {"policy_sets": []}

    def test_200_returns_body(self) -> None:
        bundle = {"policy_sets": [{"id": "set1"}], "context": {"workspace": {}}}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Authorization") == "Bearer tok"
            return httpx.Response(200, json=bundle)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        assert opa.fetch_policy_bundle(cfg, client=client) == bundle

    def test_retry_then_success(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"policy_sets": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        sleeps: list[float] = []
        cfg = _cfg()
        result = opa.fetch_policy_bundle(cfg, client=client, sleep=lambda s: sleeps.append(s))
        assert result == {"policy_sets": []}
        assert attempts["n"] == 3
        assert sleeps == [3, 3]

    def test_all_fail_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        with pytest.raises(opa.PolicyEvaluationError, match="fetch failed"):
            opa.fetch_policy_bundle(cfg, client=client, sleep=lambda s: None)

    def test_request_error_then_retry(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("nope")
            return httpx.Response(200, json={"policy_sets": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        assert opa.fetch_policy_bundle(cfg, client=client, sleep=lambda s: None) == {
            "policy_sets": []
        }


class TestEvaluateSet:
    def test_passes_when_no_deny(self, tmp_path) -> None:
        plan_json = tmp_path / "plan.json"
        plan_json.write_text('{"resource_changes": []}')
        ctx = tmp_path / "ctx.json"
        ctx.write_text('{"terrapod_context": {}}')

        def fake_run_opa(plan_json, rego_path, context_path, opa_binary):
            return 0, json.dumps({"result": [{"expressions": [{"value": {}}]}]}), ""

        with patch("terrapod.runner.phases.opa._run_opa_eval", side_effect=fake_run_opa):
            result = opa.evaluate_set(
                policy_set={
                    "id": "s1",
                    "name": "set1",
                    "enforcement_level": "mandatory",
                    "policies": [{"name": "p1", "rego": "package terrapod"}],
                },
                plan_json=plan_json,
                context_path=ctx,
                rego_dir=tmp_path,
            )
        assert result["outcome"] == "passed"
        assert result["result"]["policies"][0]["passed"] is True

    def test_fails_when_deny_present(self, tmp_path) -> None:
        plan_json = tmp_path / "plan.json"
        plan_json.write_text("{}")
        ctx = tmp_path / "ctx.json"
        ctx.write_text("{}")

        def fake_run_opa(plan_json, rego_path, context_path, opa_binary):
            return (
                0,
                json.dumps(
                    {
                        "result": [{"expressions": [{"value": {"deny": ["bad"]}}]}],
                    }
                ),
                "",
            )

        with patch("terrapod.runner.phases.opa._run_opa_eval", side_effect=fake_run_opa):
            result = opa.evaluate_set(
                policy_set={
                    "id": "s",
                    "name": "n",
                    "enforcement_level": "mandatory",
                    "policies": [{"name": "p", "rego": "x"}],
                },
                plan_json=plan_json,
                context_path=ctx,
                rego_dir=tmp_path,
            )
        assert result["outcome"] == "failed"
        assert result["result"]["policies"][0]["passed"] is False
        assert result["result"]["policies"][0]["violations"] == ["bad"]

    def test_errored_when_opa_nonzero(self, tmp_path) -> None:
        plan_json = tmp_path / "plan.json"
        plan_json.write_text("{}")
        ctx = tmp_path / "ctx.json"
        ctx.write_text("{}")

        def fake_run_opa(plan_json, rego_path, context_path, opa_binary):
            return 1, "", "syntax error in rego"

        with patch("terrapod.runner.phases.opa._run_opa_eval", side_effect=fake_run_opa):
            result = opa.evaluate_set(
                policy_set={
                    "id": "s",
                    "name": "n",
                    "enforcement_level": "mandatory",
                    "policies": [{"name": "p", "rego": "x"}],
                },
                plan_json=plan_json,
                context_path=ctx,
                rego_dir=tmp_path,
            )
        assert result["outcome"] == "errored"
        assert "syntax error" in result["result"]["policies"][0]["error"]

    def test_errored_when_plan_json_missing(self, tmp_path) -> None:
        ctx = tmp_path / "ctx.json"
        ctx.write_text("{}")
        result = opa.evaluate_set(
            policy_set={
                "id": "s",
                "name": "n",
                "enforcement_level": "mandatory",
                "policies": [{"name": "p", "rego": "x"}],
            },
            plan_json=tmp_path / "missing.json",
            context_path=ctx,
            rego_dir=tmp_path,
        )
        assert result["outcome"] == "errored"
        assert "plan JSON was not available" in result["result"]["policies"][0]["error"]


class TestPostResults:
    def test_no_api_silently_returns(self) -> None:
        cfg = _cfg(TP_API_URL="", TP_RUN_ID="")
        opa.post_results(cfg, [{"x": 1}])  # no raise

    def test_201_success(self) -> None:
        body_captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body_captured["body"] = json.loads(request.content)
            return httpx.Response(201)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        opa.post_results(cfg, [{"r": 1}], client=client)
        assert body_captured["body"] == {"results": [{"r": 1}]}

    def test_all_fail_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        with pytest.raises(opa.PolicyEvaluationError, match="POST failed"):
            opa.post_results(cfg, [{"r": 1}], client=client, sleep=lambda s: None)


class TestEvaluatePolicies:
    def test_returns_zero_when_no_sets(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"policy_sets": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        n = opa.evaluate_policies(
            cfg,
            plan_json=None,  # type: ignore[arg-type]
            work_dir=__import__("pathlib").Path("/tmp"),
            client=client,
        )
        assert n == 0
