"""Every run option reaches the Pulumi CLI, or the run does not claim it did (#1559).

A Pulumi run used to drop most of its options on the floor. The worst was
destroy: the preview phase ran `pulumi preview`, so the person approving saw an
ordinary update, and the update phase then ran `destroy --yes`. Approval of one
thing, execution of another.

These follow each option from the wire payload the API sends, through the
engine's env, to the argv the CLI is invoked with -- the whole path, because the
targeting bug lived in the gap between two of those steps: the platform sent
`target-addrs` and the engine read `target-urns`.
"""

import json
import os

import pytest

from terrapod.engines.pulumi import PulumiStrategy
from terrapod.runner.phases import pulumi_exec


def _env(attrs: dict, phase: str = "plan") -> dict[str, str]:
    """The container env a run with these attributes produces."""
    strategy = PulumiStrategy()
    options = strategy.options_from_attrs({"pulumi-stack": "default/p/dev", **attrs}, phase)
    return {e["name"]: e["value"] for e in strategy.container_env(options, None)}


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "TP_DESTROY",
        "TP_REFRESH",
        "TP_REFRESH_ONLY",
        "TP_TARGET_URNS",
        "TP_REPLACE_URNS",
        "TP_PARALLELISM",
        "TP_PULUMI_STACK",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _argv(env: dict[str, str], monkeypatch, *, plan_file: str = "", update: bool = False):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return (
        pulumi_exec.update_argv(plan_file, None)
        if update
        else pulumi_exec.preview_argv(plan_file, None)
    )


class TestDestroy:
    def test_the_preview_previews_the_destroy(self, clean_env):
        argv = _argv(_env({"is-destroy": True}), clean_env)
        assert argv[:2] == ["destroy", "--preview-only"]

    def test_the_update_destroys(self, clean_env):
        argv = _argv(_env({"is-destroy": True}, "apply"), clean_env, update=True)
        assert argv[:2] == ["destroy", "--yes"]

    def test_the_two_phases_agree(self, clean_env):
        # The bug this fixes: an update preview approved, a destroy performed.
        preview = _argv(_env({"is-destroy": True}), clean_env)
        update = _argv(_env({"is-destroy": True}, "apply"), clean_env, update=True)
        assert preview[0] == update[0] == "destroy"

    def test_a_destroy_takes_no_plan_file(self, clean_env):
        # `destroy` rejects --plan / --save-plan outright.
        preview = _argv(_env({"is-destroy": True}), clean_env, plan_file="/w/plan.json")
        update = _argv(
            _env({"is-destroy": True}, "apply"), clean_env, plan_file="/w/plan.json", update=True
        )
        assert not any(a.startswith("--save-plan") for a in preview)
        assert not any(a.startswith("--plan=") for a in update)


class TestRefreshOnly:
    def test_both_phases_are_a_refresh(self, clean_env):
        # The same operation Terraform's -refresh-only performs: preview what
        # reconciling state would adopt, then adopt exactly that.
        assert _argv(_env({"refresh-only": True}), clean_env)[:2] == ["refresh", "--preview-only"]
        assert _argv(_env({"refresh-only": True}, "apply"), clean_env, update=True)[:2] == [
            "refresh",
            "--yes",
        ]

    def test_it_takes_no_plan_file(self, clean_env):
        argv = _argv(_env({"refresh-only": True}), clean_env, plan_file="/w/plan.json")
        assert not any(a.startswith("--save-plan") for a in argv)

    def test_a_destroy_wins_if_somehow_both_are_set(self, clean_env):
        # Nothing should create such a run, but destroying is the more
        # consequential of the two: preview what would actually happen.
        argv = _argv(_env({"is-destroy": True, "refresh-only": True}), clean_env)
        assert argv[0] == "destroy"


class TestTargetingAndReplacement:
    def test_targets_reach_the_cli(self, clean_env):
        # The platform sends `target-addrs` for every engine; reading a second
        # spelling is how targeting came to be dropped on every Pulumi run.
        env = _env({"target-addrs": ["urn:pulumi:dev::p::aws:s3/bucket:Bucket::a"]})
        assert json.loads(env["TP_TARGET_URNS"]) == ["urn:pulumi:dev::p::aws:s3/bucket:Bucket::a"]
        argv = _argv(env, clean_env)
        assert argv[argv.index("--target") + 1] == "urn:pulumi:dev::p::aws:s3/bucket:Bucket::a"

    def test_several_targets_are_repeated_flags(self, clean_env):
        env = _env({"target-addrs": ["urn:a", "urn:b"]})
        argv = _argv(env, clean_env)
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--target"] == ["urn:a", "urn:b"]

    def test_replacements_reach_the_preview_and_an_unbound_update(self, clean_env):
        env = _env({"replace-addrs": ["urn:a"]})
        assert json.loads(env["TP_REPLACE_URNS"]) == ["urn:a"]
        assert "--replace" in _argv(env, clean_env)
        assert "--replace" in _argv(env, clean_env, update=True)

    def test_a_bound_update_takes_its_operations_from_the_plan(self, clean_env):
        # The plan already names what is replaced; --replace beside it would ask
        # for something the approved plan does not say.
        argv = _argv(
            _env({"replace-addrs": ["urn:a"]}), clean_env, plan_file="/w/p.json", update=True
        )
        assert "--replace" not in argv
        assert "--plan=/w/p.json" in argv

    def test_a_destroy_ignores_replacements(self, clean_env):
        argv = _argv(_env({"is-destroy": True, "replace-addrs": ["urn:a"]}), clean_env)
        assert "--replace" not in argv

    def test_junk_in_the_env_is_not_a_crash(self, clean_env):
        clean_env.setenv("TP_TARGET_URNS", "not json")
        assert "--target" not in pulumi_exec.preview_argv("", None)


class TestRefresh:
    @pytest.mark.parametrize(
        ("attrs", "expected"),
        [
            ({}, "--refresh=true"),
            ({"refresh": True}, "--refresh=true"),
            ({"refresh": False}, "--refresh=false"),
        ],
    )
    def test_refresh_is_always_explicit(self, clean_env, attrs, expected):
        # Saying nothing left the run doing whatever the stack was configured
        # for, which is not what the platform default of `refresh: true` says.
        assert expected in _argv(_env(attrs), clean_env)

    def test_the_update_says_the_same(self, clean_env):
        assert "--refresh=false" in _argv(_env({"refresh": False}, "apply"), clean_env, update=True)


class TestTheHooksARunGets:
    """A workspace's hooks are the workspace's, whatever engine it uses."""

    def _phase(self, monkeypatch, tmp_path, *, phase: str, hook_fails: str = "", rc: int = 0):
        from unittest.mock import MagicMock, patch

        from terrapod.runner import job_entrypoint
        from terrapod.runner.phases import execution_hooks

        monkeypatch.setenv("TP_PULUMI_PHASE", phase)
        monkeypatch.setenv("TP_PULUMI_EVENT_LOG", str(tmp_path / "events.json"))
        ran: list[str] = []

        def _hook(point, env=None):
            ran.append(point)
            if point == hook_fails:
                raise execution_hooks.HookError(hook_point=point, name="check", exit_code=9)

        with (
            patch("terrapod.runner.phases.execution_hooks.run_point", side_effect=_hook),
            patch("terrapod.runner.phases.platform_tool.ensure_tool", return_value="/bin/pulumi"),
            patch(
                "terrapod.runner.phases.pulumi_exec.prepare_local_stack", return_value=MagicMock()
            ),
            patch("terrapod.runner.phases.pulumi_exec.bind_plan_enabled", return_value=False),
            patch("terrapod.runner.exec_subprocess.run", return_value=MagicMock(exit_code=rc)),
            patch.object(job_entrypoint, "_report_pulumi_preview"),
            patch.object(job_entrypoint, "_hand_back_pulumi_state", return_value=rc),
        ):
            code = job_entrypoint._run_pulumi_phase(
                MagicMock(phase="plan", has_api=False, api_url="", auth_token="", plan_only=False),
                child_grace=30,
            )
        return ran, code

    def test_a_preview_runs_the_plan_hooks(self, monkeypatch, tmp_path):
        ran, code = self._phase(monkeypatch, tmp_path, phase="preview")
        assert ran == ["pre_plan", "post_plan"]
        assert code == 0

    def test_an_update_runs_the_apply_hooks(self, monkeypatch, tmp_path):
        ran, code = self._phase(monkeypatch, tmp_path, phase="update")
        assert ran == ["pre_apply", "post_apply"]
        assert code == 0

    def test_a_failing_pre_apply_hook_stops_the_update(self, monkeypatch, tmp_path):
        ran, code = self._phase(monkeypatch, tmp_path, phase="update", hook_fails="pre_apply")
        assert ran == ["pre_apply"]
        assert code == 9

    def test_a_failing_post_plan_hook_fails_the_run(self, monkeypatch, tmp_path):
        ran, code = self._phase(monkeypatch, tmp_path, phase="preview", hook_fails="post_plan")
        assert code == 9

    def test_no_post_apply_hook_after_a_failed_update(self, monkeypatch, tmp_path):
        # A hook reporting on an update that did not happen is worse than none.
        ran, code = self._phase(monkeypatch, tmp_path, phase="update", rc=1)
        assert ran == ["pre_apply"]
        assert code == 1


def test_the_wire_carries_one_spelling_for_every_engine():
    # Both engines read the same two attributes off the same payload.
    from terrapod.engines.terraform import TerraformStrategy

    payload = {"target-addrs": ["a"], "replace-addrs": ["b"], "pulumi-stack": "default/p/dev"}
    terraform = TerraformStrategy().options_from_attrs(payload, "plan")
    pulumi = PulumiStrategy().options_from_attrs(payload, "plan")
    assert terraform.target_addrs == pulumi.target_urns == ["a"]
    assert terraform.replace_addrs == pulumi.replace_urns == ["b"]


def test_nothing_terraform_sends_is_read_from_a_second_spelling():
    # A guard against the class of bug this issue is about: the engine must not
    # invent attribute names the API never sends.
    import inspect

    from terrapod.engines import pulumi as pulumi_engine

    source = inspect.getsource(pulumi_engine.PulumiStrategy.options_from_attrs)
    assert "target-urns" not in source and "replace-urns" not in source
    assert os.linesep is not None  # keeps the import honest under lint
