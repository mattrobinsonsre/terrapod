"""The runner half of shared policy evaluation (#1842).

`evaluate_set` used to run one `opa eval` per policy, each seeing only its own
file plus the run context. Shared evaluation writes the whole set — policies,
`.rego` helpers and `.yaml`/`.json` data — into one directory and evaluates it
in a single `opa eval`, which is how a helper becomes callable from another
file and how data lands under `data.<key>` the way `conftest -d` does it.

**The real-OPA test is the one that matters.** Everything else here can pass
while the feature does not work, because the claim being made is about what
OPA does with a directory, not about what this module does with a dict. It is
skipped rather than failed when no usable binary is present, and the skip says
so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from terrapod.runner.phases import opa


def _opa_binary() -> str | None:
    """A runnable `opa`, or None. Checks it actually executes: the cached
    tool under $TMPDIR is a Linux build on a macOS host, which exists and
    then fails with an exec format error."""
    for candidate in (shutil.which("opa"), "/tmp/opa-darwin", "/usr/local/bin/opa"):
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            r = subprocess.run([candidate, "version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


POLICY = """package terrapod
import rego.v1

deny contains msg if {
	some r in input.resource_changes
	c := r.change.after.cidr_block
	not is_approved(c)
	msg := sprintf("cidr %v is not approved", [c])
}
"""

HELPER = """package terrapod
import rego.v1

is_approved(c) if {
	some x in data.approved_cidrs
	x == c
}
"""


def _set(**kw) -> dict:
    base = {
        "id": "polset-1",
        "name": "network",
        "enforcement_level": "mandatory",
        "shared_evaluation": True,
        "support_files": {"data.yaml": "approved_cidrs:\n  - 10.0.0.0/8\n"},
        "policies": [{"id": "pol-1", "name": "cidr-allowlist", "rego": POLICY}],
    }
    base.update(kw)
    return base


class TestSharedEvaluationAgainstRealOpa:
    """What the feature actually claims, checked against the tool that has to
    honour it."""

    @pytest.fixture(autouse=True)
    def _binary(self):
        b = _opa_binary()
        if b is None:
            pytest.skip("no runnable opa binary — the real-OPA claim cannot be checked here")
        self.opa = b

    def _run(self, tmp_path: Path, policy_set: dict, plan: dict) -> dict:
        plan_json = tmp_path / "plan.json"
        plan_json.write_text(json.dumps(plan))
        context = tmp_path / "ctx.json"
        context.write_text(json.dumps({"terrapod_context": {"workspace": "w"}}))
        return opa.evaluate_set(
            policy_set=policy_set,
            plan_json=plan_json,
            context_path=context,
            rego_dir=tmp_path / "rego",
            opa_binary=self.opa,
        )

    def test_a_helper_in_another_file_is_callable(self, tmp_path):
        """The thing that could not be done before. `is_approved` lives in a
        deny-less helper, which the sync used to drop entirely."""
        ps = _set(
            support_files={"data.yaml": "approved_cidrs:\n  - 10.0.0.0/8\n", "helpers.rego": HELPER}
        )
        out = self._run(
            tmp_path,
            ps,
            {"resource_changes": [{"change": {"after": {"cidr_block": "192.168.0.0/16"}}}]},
        )
        assert out["outcome"] == "failed"
        assert out["result"]["policies"][0]["violations"] == ["cidr 192.168.0.0/16 is not approved"]

    def test_data_from_a_yaml_file_reaches_the_policy(self, tmp_path):
        """An approved CIDR must NOT deny — proving the allowlist was read
        from `data.yaml` rather than the rule simply never matching."""
        ps = _set(
            support_files={"data.yaml": "approved_cidrs:\n  - 10.0.0.0/8\n", "helpers.rego": HELPER}
        )
        out = self._run(
            tmp_path,
            ps,
            {"resource_changes": [{"change": {"after": {"cidr_block": "10.0.0.0/8"}}}]},
        )
        assert out["outcome"] == "passed"
        assert out["result"]["policies"][0]["violations"] == []

    def test_json_data_works_as_well_as_yaml(self, tmp_path):
        ps = _set(
            support_files={
                "data.json": json.dumps({"approved_cidrs": ["10.0.0.0/8"]}),
                "helpers.rego": HELPER,
            }
        )
        out = self._run(
            tmp_path,
            ps,
            {"resource_changes": [{"change": {"after": {"cidr_block": "10.0.0.0/8"}}}]},
        )
        assert out["outcome"] == "passed"

    def test_two_policies_share_one_helper(self, tmp_path):
        """The duplication the issue is about: before this, each policy had to
        carry its own copy of the allowlist logic."""
        ps = _set(
            support_files={
                "data.yaml": "approved_cidrs:\n  - 10.0.0.0/8\n",
                "helpers.rego": HELPER,
            },
            policies=[
                {"id": "pol-1", "name": "a", "rego": POLICY},
                {"id": "pol-2", "name": "b", "rego": POLICY.replace("cidr %v", "also %v")},
            ],
        )
        out = self._run(
            tmp_path,
            ps,
            {"resource_changes": [{"change": {"after": {"cidr_block": "192.168.0.0/16"}}}]},
        )
        assert out["outcome"] == "failed"
        assert len(out["result"]["policies"][0]["violations"]) == 2, (
            "both policies evaluated, and their denials unioned"
        )

    def test_a_broken_helper_errors_the_set_rather_than_passing_it(self, tmp_path):
        """Fail closed. A set that cannot compile must not read as clean."""
        ps = _set(support_files={"helpers.rego": "package terrapod\nthis is not rego\n"})
        out = self._run(tmp_path, ps, {"resource_changes": []})
        assert out["outcome"] == "errored"
        assert out["result"]["policies"][0]["error"]


class TestSharedEvaluationShape:
    """Shape assertions, which need no binary."""

    def test_results_are_reported_per_set(self, tmp_path):
        """Every file shares `package terrapod`, so OPA cannot say which one
        produced a given message. One entry named for the set is the honest
        shape; a per-policy breakdown would be invented."""
        out = opa._evaluate_set_together(
            set_id="polset-1",
            set_name="network",
            enforcement="mandatory",
            policies=[{"name": "a", "rego": POLICY}, {"name": "b", "rego": POLICY}],
            support_files={},
            plan_json=None,
            context_path=tmp_path / "c.json",
            rego_dir=tmp_path / "r",
        )
        assert len(out["result"]["policies"]) == 1
        assert out["result"]["policies"][0]["policy"] == "network"

    def test_it_records_that_it_shared_and_what_went_in(self, tmp_path):
        """A reader cannot see attribution, so they should at least see which
        files produced the verdict."""
        out = opa._evaluate_set_together(
            set_id="polset-1",
            set_name="network",
            enforcement="mandatory",
            policies=[{"name": "a", "rego": POLICY}, {"name": "b", "rego": POLICY}],
            support_files={},
            plan_json=None,
            context_path=tmp_path / "c.json",
            rego_dir=tmp_path / "r",
        )
        assert out["result"]["shared_evaluation"] is True
        assert out["result"]["policies"][0]["policy"] == "network"

    def test_a_missing_plan_errors_rather_than_passing(self, tmp_path):
        out = opa._evaluate_set_together(
            set_id="polset-1",
            set_name="network",
            enforcement="mandatory",
            policies=[{"name": "a", "rego": POLICY}],
            support_files={},
            plan_json=None,
            context_path=tmp_path / "c.json",
            rego_dir=tmp_path / "r",
        )
        assert out["outcome"] == "errored"

    def test_a_set_without_the_flag_still_evaluates_per_policy(self, tmp_path):
        """The default path is untouched — that is what makes this opt-in."""
        called: list[str] = []

        def _fake_eval(*, plan_json, rego_path, context_path, opa_binary="opa"):
            called.append(str(rego_path))
            return 0, json.dumps({"result": [{"expressions": [{"value": {}}]}]}), ""

        plan = tmp_path / "p.json"
        plan.write_text("{}")
        ctx = tmp_path / "c.json"
        ctx.write_text("{}")
        rego_dir = tmp_path / "r"
        rego_dir.mkdir()

        orig = opa._run_opa_eval
        opa._run_opa_eval = _fake_eval
        try:
            out = opa.evaluate_set(
                policy_set=_set(
                    shared_evaluation=False,
                    policies=[{"name": "a", "rego": POLICY}, {"name": "b", "rego": POLICY}],
                ),
                plan_json=plan,
                context_path=ctx,
                rego_dir=rego_dir,
            )
        finally:
            opa._run_opa_eval = orig

        assert len(called) == 2, "one eval per policy, as before"
        assert len(out["result"]["policies"]) == 2
        assert "shared_evaluation" not in out["result"]


class TestSupportFileNamesAreNotTrusted:
    """The names arrive from a git repository via the bundle, so they are
    attacker-influenced in the same sense the rego is."""

    @pytest.mark.parametrize(
        "name",
        ["../escape.rego", "nested/x.rego", "/abs.rego", ".hidden.rego", "notes.txt", ""],
    )
    def test_a_name_that_is_not_a_bare_policy_filename_is_refused(self, name):
        assert opa._safe_support_name(name) is None

    @pytest.mark.parametrize("name", ["data.yaml", "data.yml", "d.json", "helpers.rego"])
    def test_ordinary_names_are_accepted(self, name):
        assert opa._safe_support_name(name) == name

    def test_an_unsafe_name_is_skipped_without_losing_the_set(self, tmp_path):
        """One bad name must not cost the whole evaluation — the policies are
        still written and evaluated."""
        out = opa._evaluate_set_together(
            set_id="polset-1",
            set_name="network",
            enforcement="mandatory",
            policies=[{"name": "a", "rego": POLICY}],
            support_files={"../evil.rego": "package x", "data.yaml": "approved_cidrs: []"},
            plan_json=None,
            context_path=tmp_path / "c.json",
            rego_dir=tmp_path / "r",
        )
        # plan_json is None so it errors on that, not on the name — the point
        # is that it got that far rather than raising.
        assert out["outcome"] == "errored"
        assert not (tmp_path / "r" / "evil.rego").exists()
