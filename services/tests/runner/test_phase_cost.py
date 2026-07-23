"""Tests for terrapod.runner.phases.cost (#871).

The cost phase fetches the cached pricesheet and runs the native cost engine
over the plan JSON, writing ``cost_estimate.json`` for upload. It is best-effort
— every failure path returns ``None`` and never raises. We drive the engine for
real (a hand-verified one-EC2 pricesheet → $73/mo) with the pricesheet download
stubbed, plus each short-circuit.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from terrapod.runner.download import DownloadResult
from terrapod.runner.phases import cost
from terrapod.runner.runner_config import RunnerConfig

# One on-demand Linux EC2 instance in us-east-1 @ $0.10/hr × 730h = $73/mo,
# in the Terrapod YAML pricesheet shape pricegen emits.
_SHEET = (
    "schema: terrapod-pricesheet/v1\n"
    "currency: USD\n"
    "products:\n"
    "- service: AmazonEC2\n"
    "  family: Compute\n"
    "  match: type=aws_instance\n"
    "  pricing: service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1\n"
    "  price: '0.10'\n"
    "  price_type: t\n"
)

_PLAN = {
    "format_version": "1.2",
    "terraform_version": "1.9.0",
    "planned_values": {
        "root_module": {
            "resources": [
                {
                    "address": "aws_instance.web",
                    "type": "aws_instance",
                    "name": "web",
                    "mode": "managed",
                    "values": {"region": "us-east-1"},
                }
            ]
        }
    },
}


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


def _redirect_paths(tmp_path, monkeypatch):
    """Point the phase's fixed /tmp paths at the test's tmp dir."""
    monkeypatch.setattr(cost, "_PRICESHEET_DB", tmp_path / "prices.sqlite")
    monkeypatch.setattr(cost, "_COST_ESTIMATE_JSON", tmp_path / "cost_estimate.json")


def _write_sheet_db(output_path) -> None:
    """The endpoint serves a pre-built SQLite index (#1034); the fake download
    builds one from the sample sheet at the runner's download target."""
    import io

    from terrapod.services.cost.pricesheet_db import build_index

    build_index(io.StringIO(_SHEET), str(output_path))


def test_happy_path_writes_estimate(tmp_path, monkeypatch):
    _redirect_paths(tmp_path, monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN))

    def fake_download(url, output_path, **_kw):
        _write_sheet_db(output_path)
        return DownloadResult(ok=True, status=200)

    with patch.object(cost, "download_to_file", side_effect=fake_download):
        out = cost.estimate_cost(_cfg(), plan)

    assert out is not None
    data = json.loads(out.read_text())
    assert data["currency"] == "USD"
    assert data["total"]["min"] == 73.0
    assert data["total"]["max"] == 73.0
    assert data["resources"][0]["address"] == "aws_instance.web"


def test_disabled_skips(tmp_path, monkeypatch):
    _redirect_paths(tmp_path, monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN))
    # Would fail the test if it tried to download.
    with patch.object(cost, "download_to_file", side_effect=AssertionError("no fetch")):
        out = cost.estimate_cost(_cfg(TP_COST_ESTIMATION="false"), plan)
    assert out is None


def test_missing_plan_skips(tmp_path, monkeypatch):
    _redirect_paths(tmp_path, monkeypatch)
    with patch.object(cost, "download_to_file", side_effect=AssertionError("no fetch")):
        out = cost.estimate_cost(_cfg(), tmp_path / "absent.json")
    assert out is None


def test_pricesheet_fetch_failure_skips(tmp_path, monkeypatch):
    _redirect_paths(tmp_path, monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN))

    with patch.object(cost, "download_to_file", return_value=DownloadResult(ok=False, status=502)):
        out = cost.estimate_cost(_cfg(), plan)
    assert out is None


def test_no_api_skips(tmp_path, monkeypatch):
    _redirect_paths(tmp_path, monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN))
    # No TP_RUN_ID → has_api is False.
    cfg = RunnerConfig.from_env(env={"TP_API_URL": "https://api.example.com"})
    with patch.object(cost, "download_to_file", side_effect=AssertionError("no fetch")):
        out = cost.estimate_cost(cfg, plan)
    assert out is None


def test_pricesheet_request_sends_well_formed_bearer_header(tmp_path, monkeypatch):
    """Regression: the pricesheet fetch must send a valid ``Authorization: Bearer
    <token>`` header — NOT a doubled ``Authorization: Authorization: Bearer ...``.

    The cost phase once built the header value from ``cfg.auth_header`` (which was
    the *entire* header line ``"Authorization: Bearer <tok>"``) under the
    ``Authorization`` key, so the runner sent ``Authorization: Authorization:
    Bearer <tok>`` and every agent-run pricesheet fetch 401'd — cost estimation
    silently skipped on every server-side run. Only surfaced live (a real runner
    against a real API); the prior tests stubbed the download without asserting
    the header. Pin the exact header the request carries.
    """
    _redirect_paths(tmp_path, monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN))

    seen: dict[str, str] = {}

    def capture_download(url, output_path, headers=None, **_kw):
        seen.update(headers or {})
        _write_sheet_db(output_path)
        return DownloadResult(ok=True, status=200)

    with patch.object(cost, "download_to_file", side_effect=capture_download):
        out = cost.estimate_cost(_cfg(TP_AUTH_TOKEN="tok"), plan)

    assert out is not None
    assert seen.get("Authorization") == "Bearer tok"
    # Belt-and-braces: the value must not itself contain the header name.
    assert "Authorization:" not in seen.get("Authorization", "")
