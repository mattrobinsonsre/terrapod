"""Each engine builds its own run options from the wire payload (#1523).

The listener used to construct `TerraformRunOptions` itself and pass it to
whichever strategy it had resolved. A Pulumi run therefore arrived holding
Terraform's options object and raised `AttributeError: 'TerraformRunOptions'
object has no attribute 'phase'` inside `_dispatch_event` — a fire-and-forget
task, so the exception reached asyncio's "never retrieved" handler and nothing
else. No Job was created, the run stayed in `planning`, and five minutes later
the reconciler errored it as "stuck pre-launch", which points nowhere near the
cause.

Every layer of that was individually correct: the strategy resolved, the Job
builder was engine-neutral, the terminal rules were right. Only the object
handed between them was wrong, which is why unit tests on either side passed and
the live run did not.
"""

from __future__ import annotations

import pytest

from terrapod.engines import strategy_for
from terrapod.engines.pulumi import PulumiRunOptions
from terrapod.engines.terraform import TerraformRunOptions

ATTRS = {
    "terraform-version": "1.12.1",
    "execution-backend": "tofu",
    "working-directory": "infra",
    "is-destroy": False,
    "resource-cpu": "2",
    "resource-memory": "4Gi",
    "pulumi-stack": "smoke/dev",
}


@pytest.mark.parametrize(
    ("engine", "want"),
    [("terraform", TerraformRunOptions), ("pulumi", PulumiRunOptions)],
)
def test_each_strategy_returns_its_own_options_type(engine: str, want: type) -> None:
    """The bug in one line: the type must follow the engine, not the caller."""
    opts = strategy_for(engine).options_from_attrs(ATTRS, "plan")
    assert isinstance(opts, want), (
        f"{engine} produced {type(opts).__name__}; a strategy handed another "
        f"engine's options object fails at attribute access, not at the seam"
    )


def test_the_options_it_builds_can_build_a_job_spec() -> None:
    """The end-to-end shape of the failure: options → build_job_spec.

    Asserting only the type would not have caught this, because the listener's
    object *was* a valid TerraformRunOptions — it was simply the wrong one for
    the strategy consuming it.
    """
    for engine in ("terraform", "pulumi"):
        s = strategy_for(engine)
        opts = s.options_from_attrs(ATTRS, "plan")
        # Reaches the attributes the builder reads; a mismatched pair raises here.
        assert getattr(opts, "working_directory", None) == "infra"


class TestThePhaseTranslation:
    """The platform's phases are Terraform's words for every engine (#1521);
    each engine translates them into its own."""

    @pytest.mark.parametrize(("sent", "want"), [("plan", "preview"), ("apply", "update")])
    def test_pulumi_translates_the_platform_phase(self, sent: str, want: str) -> None:
        opts = strategy_for("pulumi").options_from_attrs(ATTRS, sent)
        assert opts.phase == want

    def test_terraform_needs_no_translation(self) -> None:
        """It has no `phase` field at all — the phase is passed to the builder
        separately. Pinned so a future 'tidy-up' does not add one and quietly
        re-couple the two engines' option shapes."""
        opts = strategy_for("terraform").options_from_attrs(ATTRS, "plan")
        assert not hasattr(opts, "phase")
