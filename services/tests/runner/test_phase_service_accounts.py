"""The per-phase runner ServiceAccounts, and the ordering that makes them work.

Two optional accounts over the base one: the apply phase's (#2189) and the
configure phase's (#2190). Renamed from `test_apply_service_account.py` when the
second arrived -- a file named for one instance of a class reads as covering only
that instance, which is the shape this repo keeps finding in its own gates.

Two things are tested, and only the first is behavioural. The second is
positional: `automountServiceAccountToken` is computed from whether an account
is set, so if the per-phase resolution happened AFTER the Job spec literal, the
mount decision would be made from the base account while the pod ran as
another. No rendered-output test can see that when both names are truthy, which
is why the ordering has a gate of its own -- the same shape as the
flush-inside-try gate.
"""

import ast
import pathlib

import pytest

from terrapod.config import RunnerConfig
from terrapod.runner import job_template
from terrapod.runner.job_template import build_job_spec

RUN_ID = "11111111-1111-1111-1111-111111111111"


def _pod(phase: str, apply_sa: str = "", configure_sa: str = "") -> dict:
    rc = RunnerConfig(
        service_account_name="base-sa",
        apply_service_account_name=apply_sa,
        configure_service_account_name=configure_sa,
    )
    spec = build_job_spec(
        run_id=RUN_ID,
        phase=phase,
        auth_secret_name="auth",
        env_vars=[],
        terraform_vars=[],
        runner_config=rc,
        namespace="terrapod",
    )
    return spec["spec"]["template"]["spec"]


class TestTheRenderedAccountPerPhase:
    @pytest.mark.parametrize("phase", ["plan", "apply", "configure"])
    def test_absent_apply_account_leaves_every_phase_on_the_base_one(self, phase):
        """The default must be byte-identical to before the feature existed."""
        pod = _pod(phase)
        assert pod["serviceAccountName"] == "base-sa"
        assert pod["automountServiceAccountToken"] is True

    def test_a_plan_never_takes_the_apply_account(self):
        assert _pod("plan", "apply-sa")["serviceAccountName"] == "base-sa"

    def test_an_apply_takes_the_apply_account(self):
        assert _pod("apply", "apply-sa")["serviceAccountName"] == "apply-sa"

    def test_the_mount_flag_follows_the_account_actually_used(self):
        """Not the base one. Both are truthy here, so this pins the wiring
        rather than the value -- see the positional test below for the case
        where only the apply account is set."""
        pod = _pod("apply", "apply-sa")
        assert pod["automountServiceAccountToken"] is True
        assert pod["serviceAccountName"] == "apply-sa"


class TestTheConfigureAccount:
    """The configure phase's own account (#2190), and the direction it defaults.

    **Absent means the PLAN's account, never the apply's**, and that is the
    whole decision rather than an implementation detail: a configure is not a
    Terraform apply and holds no cloud-mutating role by nature, so an operator
    who sets nothing gets a configure that cannot reach what an apply can.
    Falling through to the apply account would quietly hand every configure the
    cloud write principal -- a privilege change nobody asked for, in the
    direction that does not fail loudly.

    Note this is the OPPOSITE direction from apply-only variables, where a
    configure sits on the apply side (`PHASES_SEEING_APPLY_ONLY`). Both are
    right for their own axis: an apply-only variable is often the machine
    credential a configure needs, while a ServiceAccount is a cloud principal a
    configure has no reason to hold.
    """

    def test_a_configure_does_not_take_the_apply_account(self):
        """The default-direction assertion, and the one that would break
        silently: with only the apply feature configured, a fall-through would
        render a working pod with broader cloud permissions than intended."""
        assert _pod("configure", apply_sa="apply-sa")["serviceAccountName"] == "base-sa"

    def test_a_configure_takes_its_own_account_when_set(self):
        assert (
            _pod("configure", configure_sa="configure-sa")["serviceAccountName"] == "configure-sa"
        )

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    def test_the_configure_account_reaches_no_other_phase(self, phase):
        """A configure account is the narrowest of the three, so another phase
        taking it breaks that phase rather than widening it -- which is the
        failure mode that looks like a cloud problem."""
        pod = _pod(phase, configure_sa="configure-sa")
        assert pod["serviceAccountName"] != "configure-sa"

    def test_the_apply_account_still_wins_on_an_apply(self):
        """Both set, so this pins the ARM ORDER. A three-way branch written with
        the arms the other way round renders correctly for every single-feature
        test and wrongly only here."""
        assert (
            _pod("apply", apply_sa="apply-sa", configure_sa="configure-sa")["serviceAccountName"]
            == "apply-sa"
        )

    def test_a_configure_takes_its_own_even_when_the_apply_account_is_set(self):
        """The mirror of the above, and the pair is what establishes that the
        two features are independent rather than one shadowing the other."""
        assert (
            _pod("configure", apply_sa="apply-sa", configure_sa="configure-sa")[
                "serviceAccountName"
            ]
            == "configure-sa"
        )

    def test_the_mount_flag_follows_the_account_actually_used(self):
        pod = _pod("configure", configure_sa="configure-sa")
        assert pod["automountServiceAccountToken"] is True
        assert pod["serviceAccountName"] == "configure-sa"


class TestTheResolutionPrecedesTheSpecLiteral:
    """Positional, because the hazard is an ordering rather than a value."""

    def test_the_account_is_resolved_above_the_job_spec_literal(self):
        src = pathlib.Path(job_template.__file__).read_text()
        resolve = src.index("effective_service_account = runner_config.service_account_name")
        literal = src.index("    job_spec = {")
        automount = src.index('"automountServiceAccountToken": bool(effective_service_account)')
        assert resolve < literal, (
            "the per-phase account must be resolved BEFORE the Job spec literal; "
            "automountServiceAccountToken inside it reads the result"
        )
        assert literal < automount, "sanity: the mount flag lives inside the literal"

    def test_the_mount_flag_reads_the_resolved_account_not_the_raw_config(self):
        """A regression here is invisible to every rendered-output test above
        whenever both account names happen to be non-empty."""
        src = pathlib.Path(job_template.__file__).read_text()
        assert '"automountServiceAccountToken": bool(effective_service_account)' in src
        assert '"automountServiceAccountToken": bool(runner_config.service_account_name)' not in src

    def test_only_one_site_assigns_the_service_account_name(self):
        """Derived, not listed: a second assignment would be a second thing to
        drift, and the phase choice would stop being in one place."""
        tree = ast.parse(pathlib.Path(job_template.__file__).read_text())
        writes = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "serviceAccountName"
                for t in n.targets
            )
        ]
        assert len(writes) == 1, f"expected one serviceAccountName write, found {len(writes)}"
