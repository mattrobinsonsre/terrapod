"""A Pulumi preview reaches the cost engine, and Terraform does not notice (#1569).

Two halves. The first follows a preview's event log all the way to an uploaded
``cost-estimate`` artifact, driving the real cost engine with only the
pricesheet download stubbed -- because the point of the issue is a number, and a
test that asserts on the shape of a call would have passed just as happily with
the type table empty.

The second is the engine gate: adding a second engine to a path Terraform owns
must leave Terraform's own behaviour byte-identical, in every combination.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from terrapod.engines import estimates_cost
from terrapod.engines.pulumi import PulumiStrategy
from terrapod.engines.terraform import TerraformStrategy
from terrapod.runner import job_entrypoint
from terrapod.runner.download import DownloadResult
from terrapod.runner.phases import cost
from terrapod.runner.runner_config import RunnerConfig

URN = "urn:pulumi:dev::shop::aws:ec2/instance:Instance::web"

# One on-demand t3.micro in us-east-1 @ $0.10/hr x 730h = $73/mo.
_SHEET = (
    "schema: terrapod-pricesheet/v1\n"
    "currency: USD\n"
    "products:\n"
    "- service: AmazonEC2\n"
    "  family: Compute\n"
    "  match: type=aws_instance&values.instance_type=t3.micro\n"
    "  pricing: service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1\n"
    "  price: '0.10'\n"
    "  price_type: t\n"
)


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
    monkeypatch.setattr(cost, "_PRICESHEET_DB", tmp_path / "prices.sqlite")
    monkeypatch.setattr(cost, "_COST_ESTIMATE_JSON", tmp_path / "cost_estimate.json")


def _serve_sheet(url, output_path, **_kw):
    from terrapod.services.cost.pricesheet_db import build_index

    build_index(io.StringIO(_SHEET), str(output_path))
    return DownloadResult(ok=True, status=200)


def _log(tmp_path, *events, name="events.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _pre_event(op="create", urn=URN, type_="aws:ec2/instance:Instance", inputs=None):
    return {
        "resourcePreEvent": {
            "metadata": {
                "op": op,
                "urn": urn,
                "type": type_,
                "new": {
                    "urn": urn,
                    "type": type_,
                    "custom": True,
                    "inputs": inputs if inputs is not None else {"instanceType": "t3.micro"},
                },
            }
        }
    }


def _summary(**changes):
    return {"summaryEvent": {"resourceChanges": changes or {"create": 1}}}


class TestAPreviewIsPriced:
    def test_the_estimate_reaches_the_upload(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event(), _summary())
        uploaded: list[Path] = []

        with (
            patch.object(cost, "download_to_file", side_effect=_serve_sheet),
            patch(
                "terrapod.runner.phases.uploads.upload_cost_estimate",
                side_effect=lambda cfg, path: uploaded.append(path) or True,
            ),
        ):
            job_entrypoint._pulumi_cost(_cfg(), log)

        assert len(uploaded) == 1
        data = json.loads(uploaded[0].read_text())
        assert data["currency"] == "USD"
        assert data["total"]["min"] == 73.0
        assert data["resources"][0]["address"] == URN

    def test_a_preview_with_nothing_priceable_still_uploads_an_empty_estimate(
        self, tmp_path, monkeypatch
    ):
        # "Nothing here costs anything, and here is what I could not price" is a
        # useful answer. Silence is indistinguishable from the feature being off.
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(
            tmp_path,
            _pre_event(urn="urn:pulumi:d::s::aws:iam/role:Role::r", type_="aws:iam/role:Role"),
            _summary(),
        )
        uploaded: list[Path] = []

        with (
            patch.object(cost, "download_to_file", side_effect=_serve_sheet),
            patch(
                "terrapod.runner.phases.uploads.upload_cost_estimate",
                side_effect=lambda cfg, path: uploaded.append(path) or True,
            ),
        ):
            job_entrypoint._pulumi_cost(_cfg(), log)

        data = json.loads(uploaded[0].read_text())
        assert data["total"] == {"min": 0.0, "max": 0.0}
        assert data["unpriced"][0]["type"] == "aws:iam/role:Role"


class TestNothingHereFailsARun:
    """Cost is advisory. Every failure is a log line, never an exit code."""

    def test_an_unfinished_preview_neither_prices_nor_raises(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event())  # no summary event
        with patch.object(cost, "download_to_file", side_effect=AssertionError("no fetch")):
            assert job_entrypoint._pulumi_cost(_cfg(), log) is None

    def test_an_unreachable_pricesheet_neither_prices_nor_raises(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event(), _summary())
        with (
            patch.object(
                cost, "download_to_file", return_value=DownloadResult(ok=False, status=502)
            ),
            patch(
                "terrapod.runner.phases.uploads.upload_cost_estimate",
                side_effect=AssertionError("nothing to upload"),
            ),
        ):
            assert job_entrypoint._pulumi_cost(_cfg(), log) is None

    def test_a_translation_that_raises_is_swallowed(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event(), _summary())
        with patch(
            "terrapod.runner.phases.pulumi_preview.build_cost_input",
            side_effect=RuntimeError("boom"),
        ):
            assert job_entrypoint._pulumi_cost(_cfg(), log) is None

    def test_an_engine_that_raises_is_swallowed(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event(), _summary())
        with (
            patch.object(cost, "download_to_file", side_effect=_serve_sheet),
            patch("terrapod.services.cost.estimate", side_effect=RuntimeError("boom")),
        ):
            assert job_entrypoint._pulumi_cost(_cfg(), log) is None

    def test_the_api_instruction_to_skip_is_obeyed(self, tmp_path, monkeypatch):
        # The runner never self-configures cost. An operator who turned cost
        # estimation off must not be paying for a pricesheet download.
        _redirect_paths(tmp_path, monkeypatch)
        log = _log(tmp_path, _pre_event(), _summary())
        with patch.object(cost, "download_to_file", side_effect=AssertionError("no fetch")):
            assert job_entrypoint._pulumi_cost(_cfg(TP_COST_ESTIMATION="false"), log) is None


class TestThePreviewCarriesTheApisCostInstruction:
    """Before this, a Pulumi run emitted neither env var, so the runner's own
    default (on) won and `cost_estimation.enabled: false` silently did nothing."""

    def _env(self, attrs):
        strategy = PulumiStrategy()
        options = strategy.options_from_attrs({"pulumi-stack": "default/p/dev", **attrs}, "plan")
        return {e["name"]: e["value"] for e in strategy.container_env(options, None)}

    def test_off_is_relayed(self):
        assert self._env({"cost-estimation": False})["TP_COST_ESTIMATION"] == "false"

    def test_on_ships_the_fallback_region_instead(self):
        # A Pulumi AWS resource carries no region of its own -- the provider
        # holds it, and the provider is not in the event log -- so the fallback
        # is what most of them are priced in.
        env = self._env({"cost-estimation": True, "cost-default-region": "eu-west-1"})
        assert env["TP_COST_DEFAULT_REGION"] == "eu-west-1"
        assert "TP_COST_ESTIMATION" not in env

    def test_both_engines_read_the_same_two_wire_attributes(self):
        payload = {"cost-estimation": False, "cost-default-region": "eu-west-1"}
        terraform = TerraformStrategy().options_from_attrs(payload, "plan")
        pulumi = PulumiStrategy().options_from_attrs(
            {**payload, "pulumi-stack": "default/p/dev"}, "plan"
        )
        assert terraform.cost_estimation == pulumi.cost_estimation is False
        assert terraform.cost_default_region == pulumi.cost_default_region == "eu-west-1"


class TestTheEngineGate:
    def test_both_shipped_engines_are_costed(self):
        assert estimates_cost("terraform") is True
        assert estimates_cost("pulumi") is True

    def test_an_absent_engine_reads_as_terraform(self):
        # A row written before the column existed, and what the database would
        # answer for it.
        assert estimates_cost(None) is True
        assert estimates_cost("") is True

    def test_an_engine_nobody_can_vouch_for_is_not_costed(self):
        # The opposite direction from the three gate predicates, deliberately:
        # the wrong answer here is a NUMBER shown to an operator, and an
        # estimate of nothing is indistinguishable from a change that is free.
        assert estimates_cost("ansible") is False


class TestTerraformIsUntouched:
    """The gate is new; Terraform's path through it must not be."""

    def test_the_terraform_phase_still_prices_a_plan_from_a_file(self, tmp_path, monkeypatch):
        _redirect_paths(tmp_path, monkeypatch)
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "planned_values": {
                        "root_module": {
                            "resources": [
                                {
                                    "address": "aws_instance.web",
                                    "type": "aws_instance",
                                    "name": "web",
                                    "mode": "managed",
                                    "values": {
                                        "region": "us-east-1",
                                        "instance_type": "t3.micro",
                                    },
                                }
                            ]
                        }
                    }
                }
            )
        )
        with patch.object(cost, "download_to_file", side_effect=_serve_sheet):
            out = cost.estimate_cost(_cfg(), plan)
        assert out is not None
        assert json.loads(out.read_text())["total"]["min"] == 73.0

    def test_the_terraform_container_env_is_unchanged_in_every_combination(self):
        # The env a Terraform run gets is what the entrypoint acts on, so this
        # is where a regression from adding a second engine would show up.
        # Compared against the values recorded before this change.
        from unittest.mock import MagicMock

        strategy = TerraformStrategy()

        def env(attrs):
            options = strategy.options_from_attrs(attrs, "plan")
            return {e["name"]: e["value"] for e in strategy.container_env(options, MagicMock())}

        off = env({"cost-estimation": False, "cost-default-region": "eu-west-1"})
        assert off["TP_COST_ESTIMATION"] == "false"
        assert "TP_COST_DEFAULT_REGION" not in off

        on = env({"cost-estimation": True, "cost-default-region": "eu-west-1"})
        assert on["TP_COST_DEFAULT_REGION"] == "eu-west-1"
        assert "TP_COST_ESTIMATION" not in on

        default = env({})
        assert default["TP_COST_DEFAULT_REGION"] == "us-east-1"
        assert "TP_COST_ESTIMATION" not in default

    def test_the_terraform_plan_path_does_not_go_near_the_pulumi_translation(self):
        # A source guard, because the failure it prevents is silent: the
        # Terraform plan path reads `terraform show -json` and must never be
        # routed through a translation built for a different engine's log.
        import inspect

        source = inspect.getsource(job_entrypoint._run_plan_phase)
        assert "pulumi" not in source.lower()
        assert "cost.estimate_cost(cfg, _PLAN_JSON)" in source
