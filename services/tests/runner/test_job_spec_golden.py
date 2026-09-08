"""The rendered Job spec must not change across the #1488 refactor.

Phase 2 of #1407 rewrites how the Job spec is built — it touches the code path
behind every plan and every apply. Its failure mode is not a red test: it is a
Job that launches with a subtly different spec, is accepted by Kubernetes, and
then runs differently. Unit tests that assert individual fields cannot see that;
only comparing the whole rendered object can.

So this captures the spec across an option matrix and pins it byte for byte. The
golden was generated from the code *before* the refactor, which is the only order
in which such a snapshot means anything — one captured afterwards would enshrine
whatever the refactor happened to produce, including its bugs.

    UPDATE_JOB_SPEC_GOLDEN=1 pytest tests/runner/test_job_spec_golden.py

Regenerate only when a spec change is *intended*, and say in the commit what
changed and why. A diff here during a pure refactor is a defect, not a snapshot
to refresh.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib

import pytest

from tests.runner.test_job_template import _runner_config

GOLDEN = pathlib.Path(__file__).parent / "job_spec_golden.json"

#: The option combinations worth pinning. Each exercises a branch that produces
#: different container args, env, volumes or metadata — chosen from the parameter
#: list rather than by guessing at what matters.
SCENARIOS: dict[str, dict] = {
    "plan-minimal": {"phase": "plan"},
    "apply-minimal": {"phase": "apply"},
    "plan-only": {"phase": "plan", "plan_only": True},
    "destroy": {"phase": "plan", "is_destroy": True},
    "targeted": {
        "phase": "plan",
        "target_addrs": ["aws_s3_bucket.a", "aws_s3_bucket.b"],
        "replace_addrs": ["aws_instance.c"],
    },
    "refresh-options": {"phase": "plan", "refresh_only": True, "refresh": False},
    "allow-empty-apply": {"phase": "apply", "allow_empty_apply": True},
    "terragrunt": {
        "phase": "plan",
        "terragrunt_enabled": True,
        "terragrunt_version": "0.67",
        "working_directory": "envs/prod",
    },
    "terraform-backend": {
        "phase": "plan",
        "execution_backend": "terraform",
        "terraform_version": "1.9.8",
    },
    "var-files": {"phase": "plan", "var_files": ["common.tfvars", "prod.tfvars"]},
    "resources-and-parallelism": {
        "phase": "apply",
        "resource_cpu": "4",
        "resource_memory": "8Gi",
        "parallelism": 32,
        "timeout_minutes": 180,
    },
    "cost-off": {"phase": "plan", "cost_estimation": False},
    "cost-region": {"phase": "plan", "cost_default_region": "eu-west-2"},
    "hooks-and-git-auth": {
        "phase": "plan",
        "execution_hooks": [{"hook_point": "pre_init", "name": "h", "script": "echo hi"}],
        "git_auth": [
            {"kind": "git_http_auth", "url_pattern": "github.com", "username": "x", "token": "t"}
        ],
    },
    "ca-bundle": {"phase": "plan", "ca_secret_name": "tp-ca"},
    # The onboarding path, which #1488 folds in — it is currently four extra
    # parameters riding along on a general-purpose builder, and its spec must be
    # identical after they move behind the strategy.
    "onboarding": {
        "phase": "plan",
        "onboard_session_id": "onb-123",
        "onboard_provider": "aws",
        "onboard_provider_version": "5.60.0",
        "onboard_types": ["aws_s3_bucket", "aws_iam_role"],
    },
}

#: Values held fixed so a diff can only come from the scenario or the code.
BASE: dict = {
    "run_id": "run-0123456789abcdef",
    "auth_secret_name": "tprun-0123-plan-auth",
    "vars_secret_name": "tprun-0123-plan-vars",
    "env_vars": [{"key": "TF_LOG", "value": "INFO"}],
    "terraform_vars": [{"key": "region", "value": "eu-west-1", "hcl": False}],
    "namespace": "terrapod-runners",
}


def _cfg():
    """The shared mock, with the last few attributes pinned to real values.

    `_runner_config()` leaves these three unset, so they render as MagicMock
    reprs — which embed a memory address and therefore differ on every run. A
    golden built from that would fail immediately and for the wrong reason.
    Pinned here rather than in the shared helper, so no existing test changes
    behaviour because of this file.
    """
    cfg = _runner_config()
    cfg.service_account_name = "terrapod-runner"
    cfg.termination_grace_period_seconds = 120
    cfg.extra_env_from = []
    return cfg


def _render(name: str) -> dict:
    """Render one scenario through the path production uses.

    After #1488 that is the strategy: it owns the run options and composes the
    neutral builder. The *inputs* are expressed differently from before the
    refactor; the rendered spec must be byte-identical, which is the whole point
    of the golden.
    """
    from terrapod.engines.terraform import TerraformRunOptions, TerraformStrategy

    scenario = dict(SCENARIOS[name])
    option_fields = {f.name for f in dataclasses.fields(TerraformRunOptions)}
    options = TerraformRunOptions(**{k: v for k, v in scenario.items() if k in option_fields})
    neutral = dict(BASE)
    neutral.update({k: v for k, v in scenario.items() if k not in option_fields})
    return TerraformStrategy().build_job_spec(options=options, runner_config=_cfg(), **neutral)


def _render_all() -> dict[str, dict]:
    return {name: _render(name) for name in sorted(SCENARIOS)}


def test_rendered_job_specs_are_unchanged():
    current = _render_all()

    if os.environ.get("UPDATE_JOB_SPEC_GOLDEN"):
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")
        pytest.skip("golden regenerated")

    assert GOLDEN.exists(), (
        "no golden spec recorded — generate it from the PRE-refactor code with "
        "UPDATE_JOB_SPEC_GOLDEN=1, or it pins nothing"
    )
    expected = json.loads(GOLDEN.read_text())

    # Compare per scenario: a single whole-dict assertion prints an unreadable
    # diff across sixteen Job specs, and the first difference is the one worth
    # seeing.
    for name in sorted(SCENARIOS):
        got = json.loads(json.dumps(current[name], sort_keys=True, default=str))
        assert got == expected.get(name), (
            f"the rendered Job spec for {name!r} changed.\n"
            "During a pure refactor this is a defect, not a snapshot to refresh — "
            "the spec is what Kubernetes runs, and a difference here is a "
            "difference in what executes against real infrastructure."
        )


def test_the_matrix_covers_every_option():
    """A scenario set that misses an option pins nothing about it.

    Checked against the real definitions rather than trusted to be complete: the
    value of the golden is that the refactor cannot quietly change a branch
    nobody exercised, and after #1488 the options live on the dataclass while the
    neutral parameters stay on the builder — so both are checked.
    """
    import ast

    from terrapod.engines.terraform import TerraformRunOptions

    src = pathlib.Path(__file__).resolve().parents[2] / "terrapod/runner/job_template.py"
    tree = ast.parse(src.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_job_spec")
    builder_optional = {
        a.arg
        for a, d in zip(
            fn.args.args,
            [None] * (len(fn.args.args) - len(fn.args.defaults)) + list(fn.args.defaults),
            strict=True,
        )
        if d is not None
    }
    # `engine_env` is what the strategy passes in, not a knob a scenario sets.
    builder_optional.discard("engine_env")
    option_fields = {f.name for f in dataclasses.fields(TerraformRunOptions)}

    exercised = {k for s in SCENARIOS.values() for k in s} | set(BASE)
    missing = sorted((builder_optional | option_fields) - exercised)
    assert not missing, (
        "these options are never varied by the matrix, so the golden says nothing "
        f"about them: {missing}"
    )
