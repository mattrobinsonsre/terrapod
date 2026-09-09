"""A run belongs to its workspace's engine (#1523).

Found by a live run, not by a test — which is the point of recording it here.
Every unit test passed, the Job completed, and the run reported `applied`,
because a run created on a Pulumi workspace carried `engine="terraform"` and so
took the Terraform path: `tofu init` in a directory holding only a `Pulumi.yaml`
initialises an empty directory, plans nothing and applies nothing. A green run
that never invoked Pulumi at all.

Nothing downstream can recover from this. `strategy_for(run.engine)` in the
reconciler and the listener's Job construction both key on the run, so the
engine being wrong at creation makes every later decision wrong in a way that
looks like success.
"""

from __future__ import annotations

import inspect

from terrapod.services import run_service


def test_the_run_is_constructed_with_the_workspaces_engine() -> None:
    """Asserted on the source rather than by building a Run.

    `create_run` needs a workspace row, a configuration version, pool
    resolution and a live session before it reaches the constructor; the
    property worth pinning is one line of that constructor, and reading it is
    both cheaper and harder to satisfy accidentally than a mock that returns
    whatever it was told to.
    """
    src = inspect.getsource(run_service)
    assert "engine=workspace.engine," in src, (
        "Run() must inherit `engine` from its workspace. Without it every run "
        "defaults to terraform and a non-Terraform workspace's runs execute "
        "down the Terraform path, reporting success while never running the "
        "engine the workspace asked for."
    )


def test_it_is_not_hardcoded_to_a_literal() -> None:
    """A future refactor that pins the engine to a constant would pass the test
    above only if it also removed the line — this catches the other shape,
    where the field is set but always to Terraform."""
    src = inspect.getsource(run_service)
    for bad in ('engine="terraform"', "engine='terraform'"):
        assert bad not in src, (
            f"run_service hardcodes {bad} instead of using the workspace's engine"
        )
