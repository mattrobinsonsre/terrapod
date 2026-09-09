"""What the runner tells the Pulumi CLI to do (#1523).

The module had no tests. Everything here is a thing that fails *silently* when it
is wrong — the CLI carries on and does something plausible with a default — which
is why they are worth pinning rather than left to the live smoke:

  * a plugin-override pattern that matches nothing falls back to get.pulumi.com
  * a missing backend URL falls back to Pulumi's own SaaS
  * a preview and its update disagreeing about the plan file surfaces as "no
    plan file" on the update, a long way from the preview that should have
    written it
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


class TestTheBackend:
    def test_it_points_at_terrapod(self) -> None:
        env = pulumi_exec.backend_env(API)
        assert env["PULUMI_BACKEND_URL"] == f"{API}/api/terrapod/v1/pulumi"

    def test_it_uses_the_alias_prefix_not_the_canonical_one(self) -> None:
        """A runner lags the API by design, so it may be talking to a server on
        either side of #1528 — and only the alias is served by both."""
        url = pulumi_exec.backend_env(API)["PULUMI_BACKEND_URL"]
        assert "/api/terrapod/v1/" in url
        assert "/api/v1/" not in url

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        assert (
            pulumi_exec.backend_env(API + "/")["PULUMI_BACKEND_URL"]
            == pulumi_exec.backend_env(API)["PULUMI_BACKEND_URL"]
        )

    def test_no_api_url_sets_nothing(self) -> None:
        """Better to leave it unset than to export a malformed URL the CLI would
        try to reach."""
        assert pulumi_exec.backend_env("") == {}


class TestThePluginOverride:
    def test_the_pattern_is_the_catch_all(self) -> None:
        """The one that cannot miss.

        An anchored pattern that fails to match does not error — the CLI just
        uses its default host. So it works for anyone with egress and hangs for
        anyone air-gapped, which is the failure this asserts away.
        """
        value = pulumi_exec.plugin_override_env(API, "tok")["PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"]
        assert value.startswith(".*=")

    def test_it_points_at_the_package_cache_on_the_alias_prefix(self) -> None:
        value = pulumi_exec.plugin_override_env(API, "tok")["PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"]
        assert value == f".*={API}/api/terrapod/v1/package-cache/pulumi"

    def test_the_token_is_carried(self) -> None:
        assert pulumi_exec.plugin_override_env(API, "tok")["PULUMI_ACCESS_TOKEN"] == "tok"

    def test_no_api_url_sets_nothing(self) -> None:
        assert pulumi_exec.plugin_override_env("", "tok") == {}


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

    def test_a_destroy_takes_no_plan(self, monkeypatch) -> None:
        """`destroy` rejects a plan file — there is nothing to preview into one
        that it would read back."""
        monkeypatch.setenv("TP_DESTROY", "true")
        argv = pulumi_exec.update_argv("/workspace/plan.json", _cfg())
        assert argv[0] == "destroy"
        assert not any(a.startswith("--plan=") for a in argv)

    def test_the_stack_is_passed_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("TP_PULUMI_STACK", "proj/dev")
        argv = pulumi_exec.preview_argv("p", _cfg())
        assert argv[argv.index("--stack") + 1] == "proj/dev"

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
