"""What a failed configure's error message says (#2525).

A configure that failed because a task failed on a host reported only
"Runner exited with code 2". Everything an operator needed was in the log and
in `run_host_events`, and none of it reached the run.

Four properties, and the last two are the ones a per-function test misses:

* the rendering, which is where every judgement is (first failure per host, a
  bound on how many hosts are named, an `ignore_errors` failure is not a
  failure of the run);
* that `_build_failure_message` prefers it to the runner's own generic reason;
* that the RECONCILER actually passes it -- a keyword argument with a default
  is a code path every existing test skips, so the function being right says
  nothing about the route;
* that it is keyed on the PHASE and not on `run.is_configure`, which is true
  of a composite run whose apply Job is the one that failed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import run_reconciler
from terrapod.services.host_results_service import (
    MAX_SUMMARY_HOSTS,
    render_failure_summary,
)


def _event(
    host: str,
    *,
    kind: str = "failed",
    task: str = "platform : Wait for the service to start",
    msg: str = "",
    ignored: bool = False,
):
    """A `RunHostEvent`-shaped row. A SimpleNamespace rather than a MagicMock:
    an unset MagicMock attribute is truthy, so `ignored` would read as True on
    every row and the summary would always be empty."""
    from types import SimpleNamespace

    return SimpleNamespace(host=host, kind=kind, task_name=task, msg=msg, ignored=ignored, seq=0)


class TestRenderingOneFailedHost:
    """The shape the issue asks for, and the overwhelmingly common one."""

    def test_it_names_the_host_the_task_and_the_message(self):
        out = render_failure_summary(
            [
                _event(
                    "web1.example.com",
                    msg=(
                        "Failed to gather information about Pod(s) even after "
                        "waiting for 3603 seconds"
                    ),
                )
            ]
        )
        assert out == (
            '1 host failed: web1.example.com at "platform : Wait for the service to start"\n'
            "  Failed to gather information about Pod(s) even after waiting for 3603 seconds"
        )

    def test_a_host_with_no_message_still_names_the_task(self):
        out = render_failure_summary([_event("web1", msg="")])
        assert out == '1 host failed: web1 at "platform : Wait for the service to start"'

    def test_an_event_with_no_task_names_the_host_alone(self):
        """`task_name` defaults to empty on the row, so a stream that lost it
        must not render `web1 at ""`."""
        out = render_failure_summary([_event("web1", task="", msg="boom")])
        assert out == "1 host failed: web1\n  boom"


class TestRenderingSeveralHosts:
    def test_the_count_leads_and_each_host_is_named(self):
        out = render_failure_summary(
            [_event("web1", task="t1", msg="m1"), _event("web2", task="t2", msg="m2")]
        )
        assert out.splitlines()[0] == "2 hosts failed:"
        assert '  web1 at "t1"' in out
        assert "    m1" in out
        assert '  web2 at "t2"' in out

    def test_it_stops_naming_hosts_and_says_how_many_are_left(self):
        """This lands in a notification body and a list tooltip: a playbook that
        failed on four hundred hosts must not produce a four-hundred-line
        error."""
        hosts = [f"web{n}" for n in range(10)]
        out = render_failure_summary([_event(h) for h in hosts])
        assert out.splitlines()[0] == "10 hosts failed:"
        assert f"+{10 - MAX_SUMMARY_HOSTS} more" in out
        for named in hosts[:MAX_SUMMARY_HOSTS]:
            assert named in out
        # And the ones past the bound are genuinely absent, which is the half a
        # "+N more" assertion alone does not check.
        for omitted in hosts[MAX_SUMMARY_HOSTS:]:
            assert f"{omitted} at" not in out

    def test_no_more_line_when_every_host_is_named(self):
        out = render_failure_summary([_event(f"web{n}") for n in range(MAX_SUMMARY_HOSTS)])
        assert "more" not in out


class TestWhichEventRepresentsAHost:
    def test_the_first_failure_on_a_host_wins(self):
        """Ansible's own order is preserved in the rows, and the failures after
        the first on a host are usually consequences of it."""
        out = render_failure_summary(
            [
                _event("web1", task="the cause", msg="first"),
                _event("web1", task="the consequence", msg="second"),
            ]
        )
        assert "the cause" in out
        assert "the consequence" not in out
        # One host, not two: the second event must not inflate the count.
        assert out.startswith("1 host failed")


class TestAnIgnoredFailureIsNotAFailureOfTheRun:
    def test_it_is_left_out_entirely(self):
        """`ignore_errors: true` means the playbook carried on, and the recap
        counts it under `ignored` rather than `failures`. Including one would
        report hosts as having failed a run that failed for another reason."""
        assert render_failure_summary([_event("web1", ignored=True)]) is None

    def test_a_real_failure_beside_an_ignored_one_counts_once(self):
        out = render_failure_summary(
            [_event("web1", ignored=True), _event("web2", task="real", msg="m")]
        )
        assert out.startswith("1 host failed: web2")


class TestUnreachableHostsAreSummarisedToo:
    def test_the_verb_differs(self):
        """rc 4's existing message says "one or more hosts were unreachable"
        and names none of them. A host that was unreachable ran nothing, so it
        must not read as having failed."""
        out = render_failure_summary([_event("web1", kind="unreachable", msg="ssh timed out")])
        assert out.startswith("1 host unreachable: web1")
        assert "failed" not in out

    def test_both_kinds_appear_as_separate_blocks(self):
        out = render_failure_summary(
            [
                _event("web1", task="t", msg="m"),
                _event("web2", kind="unreachable", task="t", msg="m"),
            ]
        )
        blocks = out.split("\n\n")
        assert len(blocks) == 2
        assert blocks[0].startswith("1 host failed")
        assert blocks[1].startswith("1 host unreachable")

    def test_an_unknown_kind_is_not_summarised_as_failed(self):
        """Only `ROW_KINDS` become rows, but a hand-written or future row must
        not be rendered under a verb that does not describe it."""
        assert render_failure_summary([_event("web1", kind="ok")]) is None


class TestTheMessageIsOneBoundedLine:
    def test_newlines_and_tabs_are_collapsed(self):
        out = render_failure_summary([_event("web1", msg="line one\n\tline two\n")])
        assert out.endswith("  line one line two")

    def test_escape_sequences_are_removed(self):
        """Colour is forced on for a configure, so a module message can arrive
        with escapes in it, and this is rendered as plain text everywhere."""
        out = render_failure_summary([_event("web1", msg="\x1b[31mred\x1b[0m")])
        assert "\x1b" not in out
        assert out.endswith("  red")

    def test_a_long_message_is_truncated(self):
        out = render_failure_summary([_event("web1", msg="x" * 5000)])
        assert len(out) < 1000
        assert out.endswith("…")


class TestNothingToSay:
    def test_no_events_is_none(self):
        """None rather than an empty string, so the caller falls through to the
        runner's own reason instead of replacing it with nothing."""
        assert render_failure_summary([]) is None


class TestTheQueryReadsTheRunsRows:
    async def test_it_renders_what_list_events_returned(self):
        from terrapod.services import host_results_service

        run = MagicMock()
        run.id = uuid.uuid4()
        with patch.object(
            host_results_service,
            "list_events",
            AsyncMock(return_value=[_event("web1", task="t", msg="m")]),
        ):
            out = await host_results_service.failure_summary(MagicMock(), run)
        assert out == '1 host failed: web1 at "t"\n  m'


# ── The composition, and the call site ────────────────────────────────────


def _run(**kwargs):
    run = MagicMock()
    run.id = kwargs.get("id", uuid.uuid4())
    run.workspace_id = uuid.uuid4()
    run.pool_id = uuid.uuid4()
    run.status = kwargs.get("status", "configuring")
    run.job_name = "tprun-abc123-configure"
    run.job_namespace = "terrapod-runners"
    run.error_message = kwargs.get("error_message", "")
    run.runner_exit_status = kwargs.get("runner_exit_status", "error")
    run.runner_exit_code = kwargs.get("runner_exit_code", 2)
    run.peak_memory_bytes = None
    run.is_configure = kwargs.get("is_configure", True)
    run.configure_after_apply = kwargs.get("configure_after_apply", False)
    run.plan_only = False
    run.is_drift_detection = False
    run.source = "api"
    # Explicit, like the attributes above: an unset MagicMock attribute is
    # truthy, so `is_held_at_gate` would read every `planning` run as a
    # finished plan waiting at a post-plan gate and `_reconcile_one` would
    # never reach the terminal branch these tests are about.
    run.plan_finished_at = kwargs.get("plan_finished_at", None)
    run.apply_finished_at = None
    run.configure_finished_at = None
    return run


class TestTheMessageUsesIt:
    def test_the_summary_takes_the_place_of_the_runners_generic_reason(self):
        """The runner's reason for an ansible rc 2 is "the playbook failed on
        one or more hosts", which the summary says while also naming which
        hosts. Keeping both would put the general form under the specific one
        in every notification body."""
        run = _run(error_message="the playbook failed on one or more hosts")
        msg = run_reconciler._build_failure_message(
            run, "failed", host_summary='1 host failed: web1 at "t"'
        )
        assert msg == '1 host failed: web1 at "t"\n\nRunner exited with code 2'
        assert "one or more hosts" not in msg

    def test_without_a_summary_the_runners_reason_still_leads(self):
        """The fallback, which is what a run whose host-events upload never
        arrived gets."""
        run = _run(error_message="the playbook failed on one or more hosts")
        msg = run_reconciler._build_failure_message(run, "failed")
        assert msg == "the playbook failed on one or more hosts\n\nRunner exited with code 2"

    def test_an_oom_still_wins_over_the_summary(self):
        """A killed runner's hosts failed because it was killed, so the typed
        OOM message is the cause and the summary is the symptom."""
        run = _run(runner_exit_status="oom")
        run.resource_memory = "1Gi"
        msg = run_reconciler._build_failure_message(
            run, "failed", host_summary='1 host failed: web1 at "t"'
        )
        assert "OOM" in msg
        assert "web1" not in msg


class TestTheReconcilerPassesIt:
    """The call site, which no test of `_build_failure_message` can see."""

    @staticmethod
    def _patches(summary):
        return (
            patch.object(
                run_reconciler, "_configure_host_summary", AsyncMock(return_value=summary)
            ),
            patch.object(run_reconciler, "_handle_failed", new_callable=AsyncMock),
            patch(
                "terrapod.redis.client.get_job_status_from_redis", AsyncMock(return_value="failed")
            ),
            patch("terrapod.redis.client.publish_listener_event", AsyncMock()),
        )

    async def test_a_failed_configure_gets_the_summary(self):
        run = _run(status="configuring")
        summary_patch, handle_patch, status_patch, publish_patch = self._patches(
            '1 host failed: web1 at "t"'
        )
        with summary_patch as summary, handle_patch as handle, status_patch, publish_patch:
            await run_reconciler._reconcile_one(AsyncMock(), run, "terraform")
        summary.assert_awaited_once()
        assert 'host failed: web1 at "t"' in handle.call_args.args[2]

    async def test_a_failed_PLAN_is_not_asked_for_one(self):
        """Keyed on the phase. A plan has no hosts, and asking would be a
        query per failed plan on every deployment that never runs a playbook."""
        run = _run(status="planning", is_configure=False)
        summary_patch, handle_patch, status_patch, publish_patch = self._patches("unused")
        with summary_patch as summary, handle_patch as handle, status_patch, publish_patch:
            await run_reconciler._reconcile_one(AsyncMock(), run, "terraform")
        summary.assert_not_awaited()
        assert "unused" not in handle.call_args.args[2]

    async def test_a_composite_runs_failed_APPLY_is_not_asked_for_one(self):
        """`run.is_configure` is True for a composite run throughout, so keying
        on it would attach the configure's host evidence to the apply Job that
        failed before the configure ever ran."""
        run = _run(status="applying", is_configure=True, configure_after_apply=True)
        summary_patch, handle_patch, status_patch, publish_patch = self._patches("unused")
        with summary_patch as summary, handle_patch, status_patch, publish_patch:
            await run_reconciler._reconcile_one(AsyncMock(), run, "terraform")
        summary.assert_not_awaited()


class TestTheLookupNeverFailsTheTransition:
    async def test_a_raising_query_degrades_to_no_summary(self):
        """This runs on the path that records why a run failed, so a storage or
        query problem must leave the run with the bare exit code rather than
        with no terminal transition at all."""
        from terrapod.services import host_results_service

        with patch.object(
            host_results_service, "failure_summary", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            assert await run_reconciler._configure_host_summary(AsyncMock(), _run()) is None
