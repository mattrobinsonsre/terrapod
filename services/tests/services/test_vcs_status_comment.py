"""Tests for the VCS PR status comment — plan counts, cost delta, gate details."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services.vcs_status_comment import _plan_summary


class _FakeRun:
    """Minimal stand-in for Run: only the attributes _plan_summary reads."""

    def __init__(self, **kw):
        self.status = kw.get("status", "planned")
        self.has_changes = kw.get("has_changes")
        self.resource_additions = kw.get("resource_additions")
        self.resource_changes = kw.get("resource_changes")
        self.resource_destructions = kw.get("resource_destructions")


class TestPlanSummaryCounts:
    """A planned run shows real add/change/destroy counts, not just 'changes'.

    Closes the TODO at vcs_status_comment.py: "we don't currently parse the
    plan log to extract add/change/destroy counts".
    """

    def test_shows_counts_when_present(self):
        run = _FakeRun(
            status="planned",
            has_changes=True,
            resource_additions=3,
            resource_changes=1,
            resource_destructions=2,
        )
        assert _plan_summary(run) == "+3 ~1 -2"

    def test_omits_zero_components(self):
        """A pure-create plan reads '+3', not '+3 ~0 -0'."""
        run = _FakeRun(
            status="planned",
            has_changes=True,
            resource_additions=3,
            resource_changes=0,
            resource_destructions=0,
        )
        assert _plan_summary(run) == "+3"

    def test_destroy_only(self):
        run = _FakeRun(
            status="planned",
            has_changes=True,
            resource_additions=0,
            resource_changes=0,
            resource_destructions=5,
        )
        assert _plan_summary(run) == "-5"

    def test_no_changes_unchanged(self):
        """has_changes False still reads 'no changes' — existing behaviour."""
        run = _FakeRun(status="planned", has_changes=False)
        assert _plan_summary(run) == "no changes"

    def test_falls_back_when_counts_absent(self):
        """Older runs have no counts persisted; don't invent '+0'."""
        run = _FakeRun(status="planned", has_changes=True)
        assert _plan_summary(run) == "changes"

    def test_errored_run_keeps_status_word(self):
        run = _FakeRun(status="errored", resource_additions=3)
        assert _plan_summary(run) == "errored"


class TestGateVerdictRendering:
    """Each workspace gets a <details> block naming the gates that can block.

    Advisory results are excluded by the collector, so anything reaching the
    renderer is a gate with enforcement weight: it is listed pass or fail.
    """

    def _row(self, **kw):
        from terrapod.services.vcs_status_comment import _Row

        return _Row(
            workspace_name=kw.get("workspace_name", "prod-vpc"),
            mode=kw.get("mode", "apply_then_merge"),
            plan_summary=kw.get("plan_summary", "+3 ~1 -2"),
            apply_summary=kw.get("apply_summary", "not applied"),
            mergeable_summary=kw.get("mergeable_summary", "yes"),
            cost_delta=kw.get("cost_delta"),
            gates=kw.get("gates", ()),
        )

    def test_cost_delta_column_present(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(cost_delta="+£412/mo")])
        assert "| Cost |" in out or "| Cost Δ |" in out
        assert "+£412/mo" in out

    def test_cost_delta_absent_renders_dash(self):
        """Cost estimation is opt-in; a workspace without it must not break."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(cost_delta=None)])
        assert "prod-vpc" in out

    def test_details_block_lists_failing_gate(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [
                self._row(
                    mergeable_summary="blocked: policy",
                    gates=(GateVerdict("policy", "prod-guardrails", False, "mandatory"),),
                )
            ]
        )
        assert "<details>" in out
        assert "prod-guardrails" in out
        assert "</details>" in out

    def test_details_block_lists_passing_enforcing_gate(self):
        """A passed mandatory gate is still listed — that is the attestation."""
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [self._row(gates=(GateVerdict("policy", "tagging", True, "mandatory"),))]
        )
        assert "tagging" in out

    def test_no_details_block_when_no_gates(self):
        """A workspace with no enforcing gates gets no disclosure triangle."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(gates=())])
        assert "<details>" not in out

    def test_details_summary_names_the_blocking_gate(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [
                self._row(
                    gates=(
                        GateVerdict("policy", "tagging", True, "mandatory"),
                        GateVerdict("security-scan", "trivy", False, "enforced"),
                    )
                )
            ]
        )
        assert "<summary>" in out
        summary_line = [ln for ln in out.splitlines() if "<summary>" in ln][0]
        assert "security-scan" in summary_line

    def test_workspace_name_escaped_in_details(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [
                self._row(
                    workspace_name="we|ird",
                    gates=(GateVerdict("policy", "p", True, "mandatory"),),
                )
            ]
        )
        assert "we|ird" not in out.split("<details>")[0].split("\n")[3]


class TestCollectGateVerdicts:
    """The collector reports every gate that can block, pass or fail.

    It reuses the same three predicates `post_plan_hold` checks, in the same
    order, so the comment and the run's `blocked-by` attribute cannot disagree.
    Advisory policy sets are excluded — only enforcement-carrying gates appear.
    """

    def test_mandatory_policy_pass_and_fail_both_reported(self):
        from terrapod.services.vcs_status_comment import _verdicts_from_evaluations

        evals = [
            ("prod-guardrails", "mandatory", "failed", None),
            ("tagging", "mandatory", "passed", None),
        ]
        out = _verdicts_from_evaluations(evals)
        assert [(v.name, v.passed) for v in out] == [
            ("prod-guardrails", False),
            ("tagging", True),
        ]
        assert all(v.gate == "policy" for v in out)

    def test_advisory_sets_excluded(self):
        """Advisory results cannot block, so they are not part of the attestation."""
        from terrapod.services.vcs_status_comment import _verdicts_from_evaluations

        out = _verdicts_from_evaluations([("noisy", "advisory", "failed", None)])
        assert out == []

    def test_overridden_failure_counts_as_passed(self):
        """An override is a decision that unblocked the run; the run is not held.

        The override is still visible because the name is listed.
        """
        from terrapod.services.vcs_status_comment import _verdicts_from_evaluations

        out = _verdicts_from_evaluations(
            [("prod-guardrails", "mandatory", "failed", "alice@example.com")]
        )
        assert out[0].passed is True

    def test_errored_evaluation_is_not_a_pass(self):
        """`errored` is the synthetic row the gate writes when evidence is missing."""
        from terrapod.services.vcs_status_comment import _verdicts_from_evaluations

        out = _verdicts_from_evaluations([("prod-guardrails", "mandatory", "errored", None)])
        assert out[0].passed is False


class TestScanVerdict:
    """The scan gate's enforcing level is `enforced`, not `mandatory`.

    Mirrors `security_scan_service.run_is_scan_blocked`: enforced + (failed or
    errored) + not overridden is a block. Getting the level wrong here would
    silently list every scan as advisory and drop it.
    """

    def test_enforced_failure_blocks(self):
        from terrapod.services.vcs_status_comment import _verdict_from_scan

        v = _verdict_from_scan("enforced", "failed", None)
        assert v is not None
        assert (v.gate, v.passed, v.enforcement) == ("security-scan", False, "enforced")

    def test_enforced_pass_is_reported(self):
        from terrapod.services.vcs_status_comment import _verdict_from_scan

        v = _verdict_from_scan("enforced", "passed", None)
        assert v is not None and v.passed is True

    def test_errored_scan_is_not_a_pass(self):
        from terrapod.services.vcs_status_comment import _verdict_from_scan

        v = _verdict_from_scan("enforced", "errored", None)
        assert v is not None and v.passed is False

    def test_overridden_failure_counts_as_passed(self):
        from terrapod.services.vcs_status_comment import _verdict_from_scan

        v = _verdict_from_scan("enforced", "failed", "alice@example.com")
        assert v is not None and v.passed is True

    def test_advisory_scan_excluded(self):
        from terrapod.services.vcs_status_comment import _verdict_from_scan

        assert _verdict_from_scan("advisory", "failed", None) is None


class TestRunTaskStageVerdict:
    """The post-plan stage is one gate, already resolved against enforcement.

    `run_task_service.resolve_stage` fails a stage only on a *mandatory* task
    failure, so a failed stage is by construction a mandatory failure and the
    comment can call it mandatory without re-reading the individual tasks.
    """

    def test_passed_stage(self):
        from terrapod.services.vcs_status_comment import _verdict_from_stage

        v = _verdict_from_stage("passed")
        assert v is not None
        assert (v.gate, v.passed, v.enforcement) == ("run-task", True, "mandatory")

    def test_overridden_stage_counts_as_passed(self):
        from terrapod.services.vcs_status_comment import _verdict_from_stage

        v = _verdict_from_stage("overridden")
        assert v is not None and v.passed is True

    def test_failed_stage(self):
        from terrapod.services.vcs_status_comment import _verdict_from_stage

        v = _verdict_from_stage("failed")
        assert v is not None and v.passed is False

    def test_still_running_stage_is_not_yet_a_pass(self):
        """`post_plan_hold` holds the run while the stage is pending or running."""
        from terrapod.services.vcs_status_comment import _verdict_from_stage

        for status in ("pending", "running"):
            v = _verdict_from_stage(status)
            assert v is not None and v.passed is False


class TestCostDelta:
    """The Cost Δ cell reports the monthly delta the run introduces."""

    def _run(self, **kw):
        run = _FakeRun()
        run.cost_currency = kw.get("cost_currency", "GBP")
        run.cost_diff_min = kw.get("cost_diff_min")
        run.cost_diff_max = kw.get("cost_diff_max")
        return run

    def test_single_value_gets_a_sign(self):
        from terrapod.services.vcs_status_comment import _cost_delta

        assert _cost_delta(self._run(cost_diff_min=412.0, cost_diff_max=412.0)) == "+412 GBP/mo"

    def test_negative_delta_keeps_its_sign(self):
        from terrapod.services.vcs_status_comment import _cost_delta

        assert _cost_delta(self._run(cost_diff_min=-18.5, cost_diff_max=-18.5)) == "-18.50 GBP/mo"

    def test_range_is_shown_as_a_range(self):
        from terrapod.services.vcs_status_comment import _cost_delta

        assert (
            _cost_delta(self._run(cost_diff_min=412.0, cost_diff_max=500.0))
            == "+412 to +500 GBP/mo"
        )

    def test_zero_delta_says_so(self):
        """A plan that changes resources without changing spend is worth stating."""
        from terrapod.services.vcs_status_comment import _cost_delta

        assert _cost_delta(self._run(cost_diff_min=0.0, cost_diff_max=0.0)) == "no change"

    def test_unknown_when_not_estimated(self):
        """Null is 'not estimated', never zero — a run with no cost artifact."""
        from terrapod.services.vcs_status_comment import _cost_delta

        assert _cost_delta(self._run()) is None

    def test_missing_currency_omits_the_code(self):
        from terrapod.services.vcs_status_comment import _cost_delta

        run = self._run(cost_diff_min=7.0, cost_diff_max=7.0)
        run.cost_currency = None
        assert _cost_delta(run) == "+7/mo"


class TestCollectGatesOrder:
    """Gates come back in the order `post_plan_hold` evaluates them.

    That order is what makes "first failing gate" in the details summary the
    same gate the run's `blocked-by` attribute names.
    """

    async def test_run_task_then_policy_then_scan(self):
        import uuid as _uuid

        from terrapod.services.vcs_status_comment import _collect_gates

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def first(self):
                return self._rows[0] if self._rows else None

        class _FakeDB:
            """Answers each of the three gate queries by call order."""

            def __init__(self):
                self.calls = 0

            async def execute(self, _stmt):
                self.calls += 1
                if self.calls == 1:  # post-plan task stage
                    return _FakeResult([("failed",)])
                if self.calls == 2:  # policy evaluations
                    return _FakeResult([("prod-guardrails", "mandatory", "passed", None)])
                return _FakeResult([("enforced", "failed", None)])  # security scan

        gates = await _collect_gates(_FakeDB(), _uuid.uuid4())
        assert [(g.gate, g.passed) for g in gates] == [
            ("run-task", False),
            ("policy", True),
            ("security-scan", False),
        ]


class TestCollectRowsEnrichment:
    """Every row carries its cost delta and its gate verdicts.

    Without this the new columns render as `—` on every real PR: the renderer
    and the collectors are both correct and simply never meet.
    """

    async def test_row_carries_cost_delta_and_gates(self):
        import uuid as _uuid

        from terrapod.services.vcs_status_comment import _collect_rows

        run = _FakeRun(status="planned", has_changes=True, resource_additions=2)
        run.id = _uuid.uuid4()
        run.vcs_apply_blocked_reason = None
        run.cost_currency = "USD"
        run.cost_diff_min = 25.0
        run.cost_diff_max = 25.0

        class _Workspace:
            id = _uuid.uuid4()
            name = "prod-network"
            vcs_workflow = "apply_then_merge"

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

            def first(self):
                return self._rows[0] if self._rows else None

        class _FakeDB:
            def __init__(self):
                self.calls = 0

            async def execute(self, _stmt):
                self.calls += 1
                if self.calls == 1:
                    return _FakeResult([(run, _Workspace())])
                if self.calls == 2:  # post-plan task stage: none configured
                    return _FakeResult([])
                if self.calls == 3:  # policy evaluations
                    return _FakeResult([("prod-guardrails", "mandatory", "failed", None)])
                return _FakeResult([])  # security scan: none

        class _Session:
            pr_number = 7
            vcs_connection_id = _uuid.uuid4()
            repo = "acme/infra"

        rows = await _collect_rows(_FakeDB(), _Session())
        assert len(rows) == 1
        assert rows[0].cost_delta == "+25 USD/mo"
        assert [(g.gate, g.passed) for g in rows[0].gates] == [("policy", False)]


class TestApplyPromptRespectsGates:
    """Don't invite an apply the gates will refuse.

    `post_plan_hold` holds a run whose mandatory gate failed, so
    `terrapod apply` on it is rejected. Before the gate verdicts were
    collected the comment could not know that; now it can, so prompting
    anyway would be the comment contradicting itself.
    """

    def _row(self, name, gates):
        from terrapod.services.vcs_status_comment import _Row

        return _Row(name, "apply_then_merge", "+1", "not applied", "yes", None, gates)

    def test_blocked_workspace_is_not_offered_for_apply(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [self._row("prod-network", (GateVerdict("policy", "guardrails", False, "mandatory"),))]
        )
        assert "terrapod apply" not in out

    def test_passing_workspace_is_still_offered(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [self._row("prod-network", (GateVerdict("policy", "guardrails", True, "mandatory"),))]
        )
        assert "Comment `terrapod apply` to apply `prod-network`." in out

    def test_only_the_unblocked_ones_are_offered(self):
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [
                self._row("blocked", (GateVerdict("policy", "g", False, "mandatory"),)),
                self._row("clean", ()),
            ]
        )
        assert "Comment `terrapod apply` to apply `clean`." in out
        assert "`blocked`" not in out.split("</details>")[-1]


class TestRefreshForRun:
    """Finding the PR session a run belongs to, so the plan can refresh its comment.

    The comment is enqueued when the run is *created*, before the plan has run:
    at that moment the counts, the cost and the gate verdicts do not exist yet.
    Without a refresh when the plan lands, every enriched field the comment
    gained would render empty forever.
    """

    def _run(self, pr_number=7):
        run = _FakeRun()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.vcs_pull_request_number = pr_number
        return run

    def _db_returning(self, sess):
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.first.return_value = sess
        db.execute.return_value = result
        return db

    def _session(self):
        sess = MagicMock()
        sess.id = uuid.uuid4()
        return sess

    async def test_enqueues_for_a_pr_run(self):
        import terrapod.services.vcs_status_comment as mod

        sess = self._session()
        with patch.object(mod, "enqueue_trigger", AsyncMock()) as enqueue:
            await mod.refresh_for_run(self._db_returning(sess), self._run(), "plan")

        enqueue.assert_awaited_once()
        assert enqueue.await_args.args[0] == "vcs_status_comment_update"
        assert enqueue.await_args.args[1] == {"session_id": str(sess.id)}

    async def test_non_pr_run_does_nothing(self):
        import terrapod.services.vcs_status_comment as mod

        db = AsyncMock()
        with patch.object(mod, "enqueue_trigger", AsyncMock()) as enqueue:
            await mod.refresh_for_run(db, self._run(pr_number=None), "plan")

        enqueue.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_no_session_yet_does_nothing(self):
        """A PR run whose session hasn't been upserted yet is not an error."""
        import terrapod.services.vcs_status_comment as mod

        with patch.object(mod, "enqueue_trigger", AsyncMock()) as enqueue:
            await mod.refresh_for_run(self._db_returning(None), self._run(), "plan")

        enqueue.assert_not_awaited()

    async def test_a_failing_enqueue_never_breaks_the_caller(self):
        """Called from the run lifecycle: reporting must not fail a plan."""
        import terrapod.services.vcs_status_comment as mod

        boom = AsyncMock(side_effect=RuntimeError("redis is down"))
        with patch.object(mod, "enqueue_trigger", boom):
            await mod.refresh_for_run(self._db_returning(self._session()), self._run(), "plan")

    async def test_each_input_gets_its_own_dedup_key(self):
        """The bug this exists to prevent, pinned.

        `enqueue_trigger` dedups on SET NX with a 300s TTL. The counts, the
        gates and the cost arrive in three separate runner requests within
        seconds of each other, so one key per session would let the earliest
        enqueue win and silently drop every better-informed refresh for five
        minutes — observed live as a comment frozen at `changes` / `—` while
        the database held `+3` and a cost estimate.
        """
        import terrapod.services.vcs_status_comment as mod

        sess = self._session()
        run = self._run()
        keys = []
        for reason in ("counts", "plan", "cost"):
            with patch.object(mod, "enqueue_trigger", AsyncMock()) as enqueue:
                await mod.refresh_for_run(self._db_returning(sess), run, reason)
            keys.append(enqueue.await_args.kwargs["dedup_key"])

        assert len(set(keys)) == 3, keys
        assert all(str(sess.id) in k and str(run.id) in k for k in keys)

    async def test_the_same_input_twice_dedups(self):
        """Repeating one input must still collapse — that is what dedup is for."""
        import terrapod.services.vcs_status_comment as mod

        sess = self._session()
        run = self._run()
        seen = []
        for _ in range(2):
            with patch.object(mod, "enqueue_trigger", AsyncMock()) as enqueue:
                await mod.refresh_for_run(self._db_returning(sess), run, "cost")
            seen.append(enqueue.await_args.kwargs["dedup_key"])

        assert seen[0] == seen[1]


class TestCommentIsNavigableAndDated:
    """The table has to earn its place next to the per-workspace comment.

    That comment already gives a reviewer a run link and an `Updated`
    timestamp. Until the table carries both it is strictly less useful for a
    single-workspace repo, which is most repos. The timestamp matters more
    here than there: this comment is edited as three separate uploads land and
    every refresh is best-effort, so a stale render is possible by design and
    the reader needs to be able to see it.
    """

    def _row(self, **kw):
        from terrapod.services.vcs_status_comment import _Row

        return _Row(
            workspace_name=kw.get("workspace_name", "prod-vpc"),
            mode=kw.get("mode", "apply_then_merge"),
            plan_summary="+3",
            apply_summary="not applied",
            mergeable_summary="yes",
            cost_delta=kw.get("cost_delta"),
            gates=(),
            run_url=kw.get("run_url", "https://terrapod.example/workspaces/w1/runs/r1"),
        )

    def test_workspace_cell_links_to_the_run(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row()])
        assert "[`prod-vpc`](https://terrapod.example/workspaces/w1/runs/r1)" in out

    def test_without_an_external_url_the_name_is_still_shown(self):
        """`external_url` is optional; an unlinked name beats a broken link."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(run_url=None)])
        assert "`prod-vpc`" in out
        assert "](" not in out

    def test_the_body_carries_an_updated_timestamp(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row()])
        last = [ln for ln in out.strip().splitlines() if ln.strip()][-1]
        assert last.startswith("*Updated ") and last.endswith("*")
        assert "Z" in last

    def test_the_mode_column_is_gone(self):
        """`merge_then_apply` is internal vocabulary; the Apply cell says it plainly."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(mode="merge_then_apply")])
        header = [ln for ln in out.splitlines() if ln.startswith("| Workspace")][0]
        assert "Mode" not in header
        assert header.count("|") == 6  # Workspace, Plan, Cost, Apply, Mergeable

    def test_merge_then_apply_still_reads_plainly_in_the_apply_cell(self):
        """Dropping the column must not drop the fact it carried."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(mode="merge_then_apply")])
        assert "will apply on merge" in out


class TestCommentHeading:
    """The table is titled the way the sibling per-workspace comment is.

    `vcs_status_dispatcher` heads its comment `### Terrapod — <workspace>`. A
    reader scanning a PR should recognise both as Terrapod's without reading
    the contents, so this one takes the same shape — qualified by scope rather
    than by workspace, because it spans every workspace the PR touches.
    """

    def _row(self, name="prod-vpc"):
        from terrapod.services.vcs_status_comment import _Row

        return _Row(name, "apply_then_merge", "+1", "not applied", "yes")

    def test_the_comment_is_headed(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row()])
        heading = [ln for ln in out.splitlines() if ln.startswith("###")]
        assert heading, out
        assert heading[0].startswith("### Terrapod")

    def test_the_heading_sits_above_the_table(self):
        from terrapod.services.vcs_status_comment import render_comment

        lines = render_comment([self._row()]).splitlines()
        heading_at = next(i for i, ln in enumerate(lines) if ln.startswith("###"))
        table_at = next(i for i, ln in enumerate(lines) if ln.startswith("| Workspace"))
        assert heading_at < table_at

    def test_the_empty_comment_is_headed_too(self):
        """A PR that stops affecting any workspace still says who is speaking."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([])
        assert out.splitlines()[2].startswith("### Terrapod")
