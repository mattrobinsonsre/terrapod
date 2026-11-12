"""A runner token has to outlive the Job it belongs to (#2526).

It did not. `token_ttl_seconds` defaults to 3600 while a configure Job is
allowed `configure_timeout_minutes` (180), and a 66-minute configure finished
its playbook with a dead token: the log, the per-host event stream, the
oversized-payload sidecar and the resource profile -- which also carries why the
run failed -- every one of those uploads answered 401. The run was still marked
errored correctly, by the listener, so none of that loss was loud.

Three properties here:

* the arithmetic, including that it covers the windows OUTSIDE the Job's own
  deadline, which is where the uploads that matter happen;
* that the SHIPPED defaults are consistent with each other -- the defect was a
  pair of shipped numbers, not a line of logic, so a gate over the logic alone
  would have passed on it;
* that the inconsistency warns rather than refuses, and that the warning is
  reachable at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from terrapod.auth import runner_tokens
from terrapod.config import (
    RUNNER_TOKEN_LAUNCH_MARGIN_SECONDS,
    RunnerConfig,
    runner_token_ttl_for_job,
)


def _default(name: str):
    return RunnerConfig.model_fields[name].default


class TestTheBudgetCoversWhatSitsOutsideTheJobsDeadline:
    def test_it_is_the_deadline_plus_the_grace_plus_the_launch_margin(self):
        assert runner_token_ttl_for_job(180, 120) == 180 * 60 + 120 + (
            RUNNER_TOKEN_LAUNCH_MARGIN_SECONDS
        )

    def test_it_exceeds_the_deadline_it_is_given(self):
        """The property, stated without restating the formula: the token is
        minted before the Job exists and the uploads that matter happen in the
        termination grace period after it, so both windows are on the token's
        clock and not on the Job's."""
        for minutes in (1, 60, 180, 600):
            assert runner_token_ttl_for_job(minutes, 120) > minutes * 60

    def test_a_zero_or_negative_grace_does_not_shrink_it_below_the_deadline(self):
        assert runner_token_ttl_for_job(60, 0) > 60 * 60
        assert runner_token_ttl_for_job(60, -5) > 60 * 60

    def test_a_zero_or_negative_deadline_still_yields_a_usable_lifetime(self):
        """`configureTimeoutMinutes` has `minimum: 1` in the chart schema, so
        this is a floor rather than a case to support -- but a token of 0 or a
        negative one is refused at the mint with a 422, which would fail the
        run rather than correct the configuration."""
        assert runner_token_ttl_for_job(0, 120) > 0
        assert runner_token_ttl_for_job(-10, 120) > 0


class TestTheShippedDefaultsAgree:
    """The defect was a pair of shipped numbers. Derived from the model's own
    fields, so moving either one fails here rather than silently re-creating
    it."""

    def test_the_shipped_cap_clears_the_shipped_configure_budget(self):
        needed = runner_token_ttl_for_job(
            _default("configure_timeout_minutes"),
            _default("termination_grace_period_seconds"),
        )
        cap = _default("max_token_ttl_seconds")
        assert cap >= needed, (
            f"max_token_ttl_seconds={cap} is below the {needed}s a configure Job "
            f"allowed {_default('configure_timeout_minutes')} minutes needs, so a "
            "configure past the cap loses its log, its host events and its "
            "resource profile"
        )

    def test_the_pre_fix_cap_would_have_failed_that(self):
        """The control. 7200 is what shipped, and it is what this gate exists to
        refuse -- without this, the assertion above could pass on a cap that is
        merely large rather than one that clears the ceiling."""
        needed = runner_token_ttl_for_job(
            _default("configure_timeout_minutes"),
            _default("termination_grace_period_seconds"),
        )
        assert 7200 < needed

    def test_the_chart_default_matches_the_model_default(self):
        """Both legs of the config channel, because an operator gets the
        chart's number and the model's is only the fallback.

        `/app/helm` first: the chart is copied there in the test image, where
        `services/` is flattened to `/app` -- so `parents[3]` resolves to `/`
        in the container and only works in a checkout. Same idiom as
        `tests/helm/test_values_contract.py`.
        """
        import pathlib
        import re

        chart = pathlib.Path("/app/helm/terrapod")
        if not chart.exists():  # local checkout fallback (outside the image)
            chart = pathlib.Path(__file__).resolve().parents[3] / "helm" / "terrapod"
        values = (chart / "values.yaml").read_text()
        found = re.search(r"^\s*maxTokenTTLSeconds:\s*(\d+)", values, re.MULTILINE)
        assert found, "maxTokenTTLSeconds is not in values.yaml"
        assert int(found.group(1)) == _default("max_token_ttl_seconds")


class TestTheRevocationMarkerStillOutlivesTheLongestToken:
    """What a cap raise could plausibly break, and does not.

    A token stops working when its run goes terminal, not when its TTL lapses
    -- `runner_token_state` writes a revocation marker. So the marker has to
    outlive the longest token the cap permits, or a token could become
    signature-valid-and-unrevoked again after the marker expired. `_marker_ttl`
    derives from `max_token_ttl_seconds`, so raising the cap carried it along;
    its own comment states this as a requirement and nothing asserted it.
    """

    def test_the_marker_outlives_the_shipped_cap(self):
        from terrapod.auth import runner_token_state

        with patch(
            "terrapod.config.load_runner_config",
            return_value=MagicMock(max_token_ttl_seconds=_default("max_token_ttl_seconds")),
        ):
            assert runner_token_state._marker_ttl() > _default("max_token_ttl_seconds")

    def test_it_tracks_a_cap_an_operator_raised(self):
        """The derivation, not just the shipped pair: an operator who raises the
        cap for a very long configure must not lose the revocation with it."""
        from terrapod.auth import runner_token_state

        for cap in (14400, 36000, 86400):
            with patch(
                "terrapod.config.load_runner_config",
                return_value=MagicMock(max_token_ttl_seconds=cap),
            ):
                assert runner_token_state._marker_ttl() > cap


class TestTheInconsistencyWarnsRatherThanRefuses:
    @staticmethod
    def _config(*, configure_minutes: int, cap: int) -> MagicMock:
        cfg = MagicMock()
        cfg.configure_timeout_minutes = configure_minutes
        cfg.termination_grace_period_seconds = 120
        cfg.max_token_ttl_seconds = cap
        return cfg

    def test_a_cap_below_the_configure_budget_warns(self):
        with (
            patch(
                "terrapod.config.load_runner_config",
                return_value=self._config(configure_minutes=180, cap=7200),
            ),
            patch.object(runner_tokens.logger, "warning") as warn,
        ):
            runner_tokens.warn_about_phase_token_budget()
        found = self._budget_warnings(warn)
        assert len(found) == 1
        kwargs = found[0].kwargs
        assert kwargs["max_token_ttl_seconds"] == 7200
        assert kwargs["configure_timeout_minutes"] == 180
        assert kwargs["needed_seconds"] > 7200

    def test_it_does_not_raise(self):
        """The combination WORKS -- short configures are unaffected and long
        ones still run. `settings` is built at import of `terrapod.config`, so a
        raise here is every API replica in CrashLoopBackOff, which is a far
        worse answer to a configuration that is merely lossy."""
        with (
            patch(
                "terrapod.config.load_runner_config",
                return_value=self._config(configure_minutes=600, cap=60),
            ),
            patch.object(runner_tokens.logger, "warning"),
        ):
            runner_tokens.warn_about_phase_token_budget()  # must not raise

    @staticmethod
    def _budget_warnings(warn) -> list:
        """Only the warnings about the BUDGET.

        `_effective_max_ttl` logs its own warning on this same logger for a
        non-positive cap, which is a different finding -- an assertion of
        "nothing was logged" would be satisfied by neither firing and would
        also fail on the one that correctly did.
        """
        return [c for c in warn.call_args_list if "configure" in str(c.args[0])]

    def test_a_consistent_pair_says_nothing(self):
        with (
            patch(
                "terrapod.config.load_runner_config",
                return_value=self._config(configure_minutes=180, cap=14400),
            ),
            patch.object(runner_tokens.logger, "warning") as warn,
        ):
            runner_tokens.warn_about_phase_token_budget()
        assert self._budget_warnings(warn) == []

    def test_a_non_positive_cap_is_judged_against_the_effective_one(self):
        """`max_token_ttl_seconds: 0` does not mean "no cap" -- it falls back to
        the shipped default. Judging against the raw 0 would warn on every
        deployment that set it, for a cap that is in fact the shipped one."""
        with (
            patch(
                "terrapod.config.load_runner_config",
                return_value=self._config(configure_minutes=180, cap=0),
            ),
            patch.object(runner_tokens.logger, "warning") as warn,
        ):
            runner_tokens.warn_about_phase_token_budget()
        assert self._budget_warnings(warn) == []

    def test_an_unreadable_config_does_not_refuse_the_startup(self):
        """The only new way this could refuse one, so it does not. Every other
        reader of `runners.yaml` reports a config it cannot build on its own
        path; a warning about a budget is not the place to turn that into a
        CrashLoopBackOff."""
        with (
            patch(
                "terrapod.config.load_runner_config",
                side_effect=RuntimeError("runners.yaml is not readable"),
            ),
            patch.object(runner_tokens.logger, "warning") as warn,
        ):
            runner_tokens.warn_about_phase_token_budget()  # must not raise
        assert warn.call_count == 1
        assert "could not check" in str(warn.call_args.args[0])

    def test_the_lifespan_calls_it(self):
        """The call site. A warning nothing calls is a warning nobody sees, and
        the function is otherwise unreachable."""
        import ast
        import inspect

        from terrapod.api import app as app_module

        tree = ast.parse(inspect.getsource(app_module))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "warn_about_phase_token_budget" in called
