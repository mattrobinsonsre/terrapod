"""Tests for the VCS PR status comment — plan counts, cost delta, gate details."""

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _GateFakeDB:
    """Answers each gate query by INSPECTING it, not by call order.

    The previous version dispatched on a call counter, which made
    `TestCollectGatesOrder` circular: it asserted the ordering the fake itself
    fabricated, so reordering `_collect_gates` could not fail it — the fake
    would simply hand the task-stage rows to the policy query instead. Keying
    on the table each statement selects from means the assertions are about
    the code under test.
    """

    def __init__(self, *, stages=(("post_plan", "failed"),)):
        self._stages = tuple(stages)
        self.tables_queried: list[str] = []

    async def execute(self, stmt):
        table = self._table_of(stmt)
        self.tables_queried.append(table)
        if table == "task_stages":
            # Honour the statement's own stage filter. Returning every stage
            # regardless would make these tests prove only that
            # `_verdict_from_stage` labels correctly — narrowing the query back
            # to `post_plan` would still pass, which is the bug they exist for.
            wanted = self._stage_filter(stmt)
            return _FakeResult([row for row in self._stages if row[0] in wanted])
        if table == "policy_evaluations":
            return _FakeResult([("prod-guardrails", "mandatory", "passed", None)])
        if table == "security_scan_results":
            return _FakeResult([("enforced", "failed", None)])
        raise AssertionError(f"_collect_gates issued an unexpected query against {table!r}")

    @staticmethod
    def _stage_filter(stmt) -> set[str]:
        """The stage names the statement actually asks for, read off its
        compiled bind parameters."""
        params = stmt.compile().params
        wanted = set()
        for value in params.values():
            if isinstance(value, str):
                wanted.add(value)
            elif isinstance(value, (list, tuple)):
                wanted.update(v for v in value if isinstance(v, str))
        return wanted

    @staticmethod
    def _table_of(stmt) -> str:
        froms = stmt.get_final_froms()
        for f in froms:
            name = getattr(f, "name", None)
            if name:
                return str(name)
        return "unknown"

    async def get(self, _model, _pk):
        return SimpleNamespace(id=_pk, ai_policy_mode="default")


def _gate_run():
    import uuid as _uuid

    return SimpleNamespace(id=_uuid.uuid4(), workspace_id=_uuid.uuid4())


async def _gates_with_ai(*, enforcement, row, held):
    """Drive `_collect_gates` with the AI gate's three answers pinned."""
    from terrapod.services import ai_policy_service
    from terrapod.services.vcs_status_comment import _collect_gates

    with (
        patch.object(ai_policy_service, "get_evaluation", new=AsyncMock(return_value=row)),
        patch.object(ai_policy_service, "effective_enforcement", return_value=enforcement),
        patch.object(
            ai_policy_service, "run_is_held_by_ai_policy", new=AsyncMock(return_value=held)
        ),
    ):
        return await _collect_gates(_GateFakeDB(), _gate_run())


class TestCollectGatesOrder:
    """Gates come back in the order `post_plan_hold` evaluates them.

    That order is what makes "first failing gate" in the details summary the
    same gate the run's `blocked-by` attribute names.
    """

    async def test_run_task_then_policy_then_scan_then_ai(self):
        gates = await _gates_with_ai(
            enforcement="mandatory", row=SimpleNamespace(outcome="failed"), held=True
        )
        assert [(g.gate, g.passed) for g in gates] == [
            ("run-task", False),
            ("policy", True),
            ("security-scan", False),
            ("ai-policy", False),
        ]


class TestTheAIPolicyGateAppearsInTheComment:
    """It did not, and that was the whole defect: `_collect_gates` read three
    gates and `post_plan_hold` checks four. A run this gate was
    holding rendered as all-green AND was offered a `terrapod apply` that the
    gate would refuse -- the comment contradicting the platform."""

    async def test_a_hold_with_no_verdict_yet_is_reported_as_blocking(self):
        """The state with no row at all. Keying on the row reports it clear,
        which is precisely the run that most needs the comment to speak up:
        nothing moves until a person acts."""
        gates = await _gates_with_ai(enforcement="mandatory", row=None, held=True)
        ai = [g for g in gates if g.gate == "ai-policy"]
        assert len(ai) == 1
        assert ai[0].passed is False
        assert "awaiting verdict" in ai[0].name

    async def test_a_passing_verdict_is_listed_as_an_attestation(self):
        gates = await _gates_with_ai(
            enforcement="mandatory", row=SimpleNamespace(outcome="passed"), held=False
        )
        ai = [g for g in gates if g.gate == "ai-policy"]
        assert [(g.passed, g.name) for g in ai] == [(True, "AI policy gate")]

    async def test_an_advisory_gate_is_left_out(self):
        """Advisory cannot hold a run, so listing it would dilute an
        attestation meant to say "these are the gates with teeth"."""
        gates = await _gates_with_ai(
            enforcement="advisory", row=SimpleNamespace(outcome="failed"), held=False
        )
        assert not [g for g in gates if g.gate == "ai-policy"]

    async def test_an_off_gate_is_left_out(self):
        gates = await _gates_with_ai(enforcement="off", row=None, held=False)
        assert not [g for g in gates if g.gate == "ai-policy"]

    async def test_a_mandatory_gate_not_ruling_on_this_run_is_left_out(self):
        """Mandatory deployment-wide, but exempt for this run (plan-only, or
        no criteria and no threshold): no row and not held. Attesting to a
        gate that never looked would be a false assurance."""
        gates = await _gates_with_ai(enforcement="mandatory", row=None, held=False)
        assert not [g for g in gates if g.gate == "ai-policy"]

    def test_a_held_run_is_not_offered_an_apply(self):
        """The consequence the reviewer actually sees."""
        from terrapod.services.vcs_status_comment import GateVerdict, _Row, render_comment

        row = _Row(
            workspace_name="prod-net",
            mode="apply_then_merge",
            plan_summary="+ 1",
            apply_summary="not applied",
            mergeable_summary="yes",
            gates=(
                GateVerdict("ai-policy", "AI policy gate (awaiting verdict)", False, "mandatory"),
            ),
        )
        body = render_comment([row])
        assert "terrapod apply" not in body
        assert "AI policy gate" in body


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
        run.workspace_id = _uuid.uuid4()
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

            async def get(self, _model, _pk):
                return _Workspace()

        class _Session:
            pr_number = 7
            vcs_connection_id = _uuid.uuid4()
            repo = "acme/infra"
            # Not merged yet, so no post-merge run to look for (#1878).
            merge_commit_sha = None

        from terrapod.services import ai_policy_service

        with patch.object(ai_policy_service, "effective_enforcement", return_value="off"):
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


class TestAllThreeRunTaskBoundariesReachTheComment:
    """#1837 added `pre_plan` and `pre_apply`; the comment knew only
    `post_plan`.

    A run held at `pre_apply` therefore came back with NO gates, so `blocked`
    was False and the comment offered "Comment `terrapod apply`" for a run
    `confirm_run` would refuse. The refusal posted nothing, so the reviewer
    commented, got a success reaction, and watched the same invitation
    re-render. `apply_then_merge` plus a mandatory `pre_apply` task is the
    headline use case for #1837, so that is the intended configuration.
    """

    async def _gates(self, stages):
        from terrapod.services import ai_policy_service
        from terrapod.services.vcs_status_comment import _collect_gates

        with (
            patch.object(ai_policy_service, "get_evaluation", new=AsyncMock(return_value=None)),
            patch.object(ai_policy_service, "effective_enforcement", return_value="off"),
            patch.object(
                ai_policy_service, "run_is_held_by_ai_policy", new=AsyncMock(return_value=False)
            ),
        ):
            return await _collect_gates(_GateFakeDB(stages=stages), _gate_run())

    @pytest.mark.parametrize(
        ("stage", "label"),
        [
            ("pre_plan", "pre-plan tasks"),
            ("post_plan", "post-plan tasks"),
            ("pre_apply", "pre-apply tasks"),
        ],
    )
    async def test_a_failed_stage_at_any_boundary_is_reported(self, stage, label):
        gates = await self._gates([(stage, "failed")])
        task_gates = [g for g in gates if g.gate == "run-task"]
        assert len(task_gates) == 1
        assert task_gates[0].name == label
        assert task_gates[0].passed is False, (
            "a run held at this boundary would be reported as having nothing wrong"
        )

    async def test_a_passing_stage_is_listed_as_an_attestation(self):
        gates = await self._gates([("pre_apply", "passed")])
        task_gates = [g for g in gates if g.gate == "run-task"]
        assert task_gates and task_gates[0].passed is True

    async def test_every_boundary_gets_its_own_verdict(self):
        gates = await self._gates(
            [("pre_plan", "passed"), ("post_plan", "passed"), ("pre_apply", "failed")]
        )
        names = [g.name for g in gates if g.gate == "run-task"]
        assert names == ["pre-plan tasks", "post-plan tasks", "pre-apply tasks"]

    async def test_a_re_driven_stage_is_not_counted_twice(self):
        """A re-driven run can accumulate more than one row per boundary; the
        earliest is the live one, and two verdicts for one stage would read as
        two gates."""
        gates = await self._gates([("post_plan", "failed"), ("post_plan", "passed")])
        task_gates = [g for g in gates if g.gate == "run-task"]
        assert len(task_gates) == 1
        assert task_gates[0].passed is False


class TestThePostMergeRunReachesTheComment:
    """#1878: the PR's last word on its own change was a prediction.

    A `merge_then_apply` row renders "will apply on merge" — and then the merge
    happens, the apply runs, and the comment goes on saying "will apply on
    merge" for ever, because the session is no longer open and nothing refreshes
    it. Whoever reviewed the change has to go and find the run themselves to
    learn whether the thing they approved actually landed.
    """

    def _row(self, **over):
        from terrapod.services.vcs_status_comment import _Row

        base = {
            "workspace_name": "prod",
            "mode": "merge_then_apply",
            "plan_summary": "+3 ~1",
            "apply_summary": "—",
            "mergeable_summary": "yes",
        }
        return _Row(**{**base, **over})

    def test_before_the_merge_it_still_promises(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row()])
        assert "will apply on merge" in out

    def test_after_the_merge_it_reports(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([self._row(post_merge_summary="applied")])
        assert "applied" in out
        # The promise is REPLACED, not joined by the outcome — leaving both
        # would be the comment asserting a future that has already happened.
        assert "will apply on merge" not in out

    def test_the_outcome_links_to_the_run_that_produced_it(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment(
            [self._row(post_merge_summary="errored", post_merge_url="https://tp/runs/r1")]
        )
        assert "[errored](https://tp/runs/r1)" in out

    def test_an_apply_then_merge_row_gains_the_outcome_too(self):
        """Additive detail there rather than a correction: that row already
        showed an apply, but it showed the PR-head one."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment(
            [
                self._row(
                    mode="apply_then_merge", apply_summary="applied", post_merge_summary="errored"
                )
            ]
        )
        assert "errored" in out

    def test_a_failure_after_merge_is_not_reported_as_success(self):
        from terrapod.services.vcs_status_comment import _post_merge_summary

        assert _post_merge_summary(SimpleNamespace(status="errored")) == "errored"
        assert _post_merge_summary(SimpleNamespace(status="applied")) == "applied"

    def test_a_run_still_going_says_so(self):
        from terrapod.services.vcs_status_comment import _post_merge_summary

        assert _post_merge_summary(SimpleNamespace(status="applying")) == "applying"
        assert _post_merge_summary(SimpleNamespace(status="planning")) == "running"

    def test_a_held_apply_is_not_mistaken_for_a_finished_one(self):
        """`planned` means the apply did not happen — a gate is holding it, or
        the workspace does not auto-apply. Reporting that as `applied` would be
        the exact failure this issue exists to fix, one state further on."""
        from terrapod.services.vcs_status_comment import _post_merge_summary

        assert _post_merge_summary(SimpleNamespace(status="planned")) == "awaiting apply"

    def test_no_run_yet_leaves_the_row_exactly_as_it_was(self):
        from terrapod.services.vcs_status_comment import _post_merge_summary

        assert _post_merge_summary(None) == ""


class TestAMergedSessionIsStillEditable:
    """The guard that froze the comment (#1878).

    `handle_vcs_status_comment_update` returned on any session that was not
    `open`, and the poller closes the session the moment the PR leaves the open
    list — which is strictly before the post-merge run finishes. So the one
    update this feature exists to make was the one the handler refused.
    """

    def test_open_and_merged_are_live_and_closed_is_not(self):
        from terrapod.services.vcs_status_comment import _LIVE_SESSION_STATES

        assert "open" in _LIVE_SESSION_STATES
        assert "merged" in _LIVE_SESSION_STATES
        # A PR abandoned without merging sets off no runs; there is nothing
        # further to learn, so its comment is left alone.
        assert "closed" not in _LIVE_SESSION_STATES


# ── one PR, one comment (#1940) ───────────────────────────────────────
#
# Terrapod used to put two kinds of comment on a PR with opposite update
# semantics: this status table, edited in place for ever, and a per-workspace
# comment whose identity included the commit SHA, so a push could never match
# the previous one and always posted a new comment. The per-workspace
# comments multiplied as workspaces x pushes — four workspaces and four
# pushes measured seventeen Terrapod comments on one PR.
#
# The narrative those comments carried now folds into this table's own
# per-workspace block, and this is the only comment Terrapod writes.


def _summary(**kw):
    """A PlanSummary stand-in. Plain object, not a Mock: a Mock answers every
    attribute, so a renderer reading the wrong field would still pass."""
    from types import SimpleNamespace

    base = {
        "status": "ready",
        "kind": "plan_summary",
        "risk_level": "medium",
        "description": "Widens the subnet group to a second AZ.",
        "risk_factors": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _row(**kw):
    from terrapod.services.vcs_status_comment import _Row

    base = {
        "workspace_name": "prod-vpc",
        "mode": "apply_then_merge",
        "plan_summary": "+3 ~1 -2",
        "apply_summary": "not applied",
        "mergeable_summary": "yes",
    }
    base.update(kw)
    return _Row(**base)


class TestTheNarrativeIsInTheTable:
    """The AI summary renders inside the workspace's own block, not beside it.

    This is what makes the second comment unnecessary. If the narrative stopped
    reaching this comment the feature would not break loudly — the table would
    simply stop carrying it, and the content would be gone rather than
    relocated, so each half is asserted.
    """

    def test_the_narrative_renders_in_the_comment(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([_row(ai_summary=_summary())])
        assert "AI summary" in out
        assert "Widens the subnet group" in out

    def test_the_risk_pill_is_in_the_summary_line_so_triage_needs_no_click(self):
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment([_row(ai_summary=_summary(risk_level="critical"))])
        line = [ln for ln in out.splitlines() if "<summary>" in ln][0]
        assert "critical" in line
        assert "prod-vpc" in line

    def test_a_failure_analysis_is_labelled_as_one(self):
        """Same row, same fields, opposite meaning — `kind` is the only thing
        that distinguishes an explanation of a failure from a description of a
        change, and mislabelling it would invert the comment's message."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment(
            [_row(ai_summary=_summary(kind="failure_analysis", risk_factors=[{"title": "Fix it"}]))]
        )
        assert "Failure analysis" in out
        assert "AI summary" not in out
        assert "Suggested fixes:" in out

    def test_narrative_and_gates_share_one_disclosure_triangle(self):
        """Two blocks per workspace would be the split this issue closes, in
        miniature: the same two clicks, on the same comment."""
        from terrapod.services.vcs_status_comment import GateVerdict, render_comment

        out = render_comment(
            [
                _row(
                    ai_summary=_summary(),
                    gates=(GateVerdict("policy", "tagging", True, "mandatory"),),
                )
            ]
        )
        assert out.count("<details>") == 1, out
        assert "Widens the subnet group" in out
        assert "tagging" in out

    def test_an_unready_summary_discloses_nothing(self):
        """pending / skipped / errored have nothing a reviewer can act on."""
        from terrapod.services.vcs_status_comment import render_comment

        for status in ("pending", "skipped", "errored"):
            out = render_comment([_row(ai_summary=_summary(status=status))])
            assert "<details>" not in out, status

    def test_a_malformed_risk_factor_is_skipped_not_dumped(self):
        """`risk_factors` is model-authored JSON, so a non-mapping element is
        possible; a PR comment is the wrong place to surface its repr."""
        from terrapod.services.vcs_status_comment import render_comment

        out = render_comment(
            [_row(ai_summary=_summary(risk_factors=["just a string", {"title": "Real one"}]))]
        )
        assert "just a string" not in out
        assert "Real one" in out


class _FakeRedis:
    """In-memory async Redis stand-in, moved here with the comment machinery.

    Implements only what `_post_or_update` uses, with real SETNX semantics so
    the lock is actually exercised: `set(nx=)` returns False when the key
    exists, and `eval` emulates the compare-and-delete release rather than
    running the Lua.

    Every operation yields. Without that, `asyncio.gather` runs each coroutine
    to completion in one slice and a contention test cannot contend.
    """

    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        await asyncio.sleep(0)
        if nx and key in self._store:
            return False
        self._store[key] = str(value)
        return True

    async def get(self, key):
        await asyncio.sleep(0)
        return self._store.get(key)

    async def delete(self, key):
        await asyncio.sleep(0)
        self._store.pop(key, None)
        return 1

    async def eval(self, script, numkeys, key, arg):
        await asyncio.sleep(0)
        if self._store.get(key) == arg:
            self._store.pop(key, None)
            return 1
        return 0


class _FakeGitHub:
    """Records what was posted, and serves it back from the listing — so a
    marker search sees what a create actually wrote."""

    def __init__(self):
        self.posted: list[dict] = []
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []
        self.listed = 0

    async def create(self, conn, owner, repo, pr_number, body):
        # Must yield: without it each coroutine runs create-to-completion in
        # one slice and `asyncio.gather` cannot interleave, so a concurrency
        # test passes with the lock removed.
        await asyncio.sleep(0)
        self.created.append(body)
        cid = 100 + len(self.created)
        self.posted.append({"id": cid, "body": body})
        return cid

    async def update(self, conn, owner, repo, comment_id, body):
        await asyncio.sleep(0)
        if not any(c["id"] == comment_id for c in self.posted):
            raise RuntimeError("no such comment")
        self.updated.append((comment_id, body))
        for c in self.posted:
            if c["id"] == comment_id:
                c["body"] = body
        return comment_id

    async def list(self, conn, owner, repo, pr_number):
        await asyncio.sleep(0)
        self.listed += 1
        return self.posted


class _CommentHarness:
    """Drives `_post_or_update` against the fakes above."""

    def __init__(self):
        self.redis = _FakeRedis()
        self.gh = _FakeGitHub()
        self.conn = SimpleNamespace(id=uuid.uuid4(), provider="github")

    def patches(self):
        from terrapod.services import vcs_status_comment as mod

        return (
            patch("terrapod.redis.client.get_redis_client", return_value=self.redis),
            patch.object(mod.github_service, "list_pr_comments", new=self.gh.list),
            patch.object(mod.github_service, "create_pr_comment", new=self.gh.create),
            patch.object(mod.github_service, "update_pr_comment", new=self.gh.update),
        )

    async def post(self, body, recorded_id=None):
        from terrapod.services.vcs_status_comment import _COMMENT_MARKER, _post_or_update

        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await _post_or_update(
                self.conn, "org/repo", 7, f"{_COMMENT_MARKER}\n{body}", recorded_id
            )


class TestOnePrOneComment:
    """A push edits the comment; it does not add one.

    The inverse of this was asserted deliberately before #1940 — the commit
    SHA was part of the comment's identity, so `test_a_second_push_gets_its
    _own_comment` was a passing test of the behaviour this issue removes.
    """

    @pytest.mark.asyncio
    async def test_a_second_push_edits_the_one_comment(self):
        h = _CommentHarness()
        first = await h.post("plan for sha1111")
        second = await h.post("plan for sha2222")

        assert len(h.gh.created) == 1, h.gh.created
        assert len(h.gh.updated) == 1, h.gh.updated
        assert first == second
        assert "sha2222" in h.gh.posted[0]["body"]

    @pytest.mark.asyncio
    async def test_four_workspaces_and_four_pushes_make_one_comment(self):
        """The measurement from the issue: this used to be seventeen."""
        h = _CommentHarness()
        for _push in range(4):
            for _ws in range(4):
                await h.post("a refresh")
        assert len(h.gh.created) == 1
        assert len(h.gh.posted) == 1

    @pytest.mark.asyncio
    async def test_the_recorded_id_spares_the_listing(self):
        """The common path must not cost a comment listing on every refresh."""
        h = _CommentHarness()
        recorded = await h.post("first")
        before = h.gh.listed
        await h.post("second", recorded_id=recorded)
        assert h.gh.listed == before

    @pytest.mark.asyncio
    async def test_a_deleted_comment_is_found_again_by_its_marker(self):
        """`_COMMENT_MARKER` was written into every comment for years with a
        docstring saying it existed as this fallback, while nothing read it.
        A stale recorded id used to mean a second comment."""
        h = _CommentHarness()
        await h.post("first")
        real_id = h.gh.posted[0]["id"]

        got = await h.post("second", recorded_id="999999")

        assert got == str(real_id)
        assert len(h.gh.created) == 1, "a stale id posted a second comment"
        assert h.gh.listed >= 1, "the marker search did not run"

    @pytest.mark.asyncio
    async def test_concurrent_refreshes_create_one_comment_not_several(self):
        """Three runner uploads land separately and a module PR refreshes once
        per consumer, so concurrent refreshes are the normal case, not an edge.
        Without the per-PR lock each races through "nothing found" and posts."""
        from terrapod.services.vcs_status_comment import _COMMENT_MARKER, _post_or_update

        h = _CommentHarness()
        with contextlib.ExitStack() as stack:
            for p in h.patches():
                stack.enter_context(p)
            await asyncio.gather(
                *(
                    _post_or_update(h.conn, "org/repo", 7, f"{_COMMENT_MARKER}\nrefresh {i}")
                    for i in range(5)
                )
            )
        assert len(h.gh.created) == 1, h.gh.created


class TestTheSessionRecordsTheCommentId:
    """The handler must persist the id `_post_or_update` ends up using.

    Without this the recorded id never updates, so every refresh falls through
    to a comment listing — correct, but a listing per refresh on every PR.
    """

    @staticmethod
    def _db(sess, conn):
        db = MagicMock()

        async def _get(model, _id):
            from terrapod.db.models import PRSession, VCSConnection

            return {PRSession: sess, VCSConnection: conn}.get(model)

        db.get = AsyncMock(side_effect=_get)
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_the_id_is_written_back_to_the_session(self):
        from terrapod.services import vcs_status_comment as mod

        h = _CommentHarness()
        sess = SimpleNamespace(
            id=uuid.uuid4(),
            state="open",
            repo="org/repo",
            pr_number=7,
            vcs_connection_id=h.conn.id,
            status_comment_id=None,
            merge_commit_sha=None,
        )
        db = self._db(sess, h.conn)

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False

        with contextlib.ExitStack() as stack:
            for p in h.patches():
                stack.enter_context(p)
            stack.enter_context(patch.object(mod, "get_db_session", return_value=_Ctx()))
            stack.enter_context(
                patch.object(mod, "_collect_rows", new=AsyncMock(return_value=[_row()]))
            )
            await mod.handle_vcs_status_comment_update({"session_id": str(sess.id)})

        assert sess.status_comment_id is not None
        assert sess.status_comment_id == str(h.gh.posted[0]["id"])
        db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_post_leaves_the_recorded_id_alone(self):
        """Clearing it on a transient failure would make the next refresh
        believe there is no comment and post a second one."""
        from terrapod.services import vcs_status_comment as mod

        h = _CommentHarness()
        sess = SimpleNamespace(
            id=uuid.uuid4(),
            state="open",
            repo="org/repo",
            pr_number=7,
            vcs_connection_id=h.conn.id,
            status_comment_id="4242",
            merge_commit_sha=None,
        )
        db = self._db(sess, h.conn)

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False

        async def _boom(*a, **k):
            raise RuntimeError("GitHub is having a day")

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("terrapod.redis.client.get_redis_client", return_value=h.redis)
            )
            stack.enter_context(patch.object(mod.github_service, "update_pr_comment", new=_boom))
            stack.enter_context(patch.object(mod.github_service, "list_pr_comments", new=_boom))
            stack.enter_context(patch.object(mod.github_service, "create_pr_comment", new=_boom))
            stack.enter_context(patch.object(mod, "get_db_session", return_value=_Ctx()))
            stack.enter_context(
                patch.object(mod, "_collect_rows", new=AsyncMock(return_value=[_row()]))
            )
            await mod.handle_vcs_status_comment_update({"session_id": str(sess.id)})

        assert sess.status_comment_id == "4242"


class TestAModulePrAlsoGetsOneComment:
    """A module PR used to carry one comment per consuming workspace.

    Keyed on the workspace id, so a module with ten linked workspaces put ten
    comments on one PR — and each was reposted per push, for the same
    per-commit-identity reason. They are rows of one table now.
    """

    @pytest.mark.asyncio
    async def test_every_consumer_is_a_row_of_one_comment(self):
        from terrapod.services import vcs_status_comment as mod

        h = _CommentHarness()
        rows = [_row(workspace_name="app-a"), _row(workspace_name="app-b")]
        with contextlib.ExitStack() as stack:
            for p in h.patches():
                stack.enter_context(p)
            stack.enter_context(
                patch.object(mod, "_collect_module_rows", new=AsyncMock(return_value=rows))
            )
            # Once per consuming workspace, as the module-impact path calls it.
            for _ in range(2):
                await mod.refresh_module_pr_comment(
                    MagicMock(), h.conn, "org/module", 42, [uuid.uuid4(), uuid.uuid4()]
                )

        assert len(h.gh.created) == 1, h.gh.created
        body = h.gh.posted[0]["body"]
        assert "app-a" in body and "app-b" in body

    @pytest.mark.asyncio
    async def test_no_rows_posts_nothing_rather_than_an_empty_table(self):
        from terrapod.services import vcs_status_comment as mod

        h = _CommentHarness()
        with contextlib.ExitStack() as stack:
            for p in h.patches():
                stack.enter_context(p)
            stack.enter_context(
                patch.object(mod, "_collect_module_rows", new=AsyncMock(return_value=[]))
            )
            await mod.refresh_module_pr_comment(
                MagicMock(), h.conn, "org/module", 42, [uuid.uuid4()]
            )
        assert h.gh.created == []

    @pytest.mark.asyncio
    async def test_a_failure_does_not_escape_onto_the_analysis_path(self):
        from terrapod.services import vcs_status_comment as mod

        h = _CommentHarness()
        with patch.object(
            mod, "_collect_module_rows", new=AsyncMock(side_effect=RuntimeError("nope"))
        ):
            await mod.refresh_module_pr_comment(
                MagicMock(), h.conn, "org/module", 42, [uuid.uuid4()]
            )

    @pytest.mark.asyncio
    async def test_no_linked_workspaces_queries_nothing(self):
        from terrapod.services.vcs_status_comment import _collect_module_rows

        db = MagicMock()
        db.execute = AsyncMock(side_effect=AssertionError("must not query"))
        assert await _collect_module_rows(db, [], 42) == []

    @pytest.mark.asyncio
    async def test_the_query_is_scoped_to_module_test_runs(self):
        """PR numbers are per-repository and nothing on a run records which
        repository its number came from, so without the source filter a
        workspace's own PR #7 would appear on a module's PR #7.

        Asserted against the compiled statement: a fake that returns rows
        regardless of the query would pass with the filter removed.
        """
        from terrapod.services.vcs_status_comment import _collect_module_rows

        seen = {}

        async def _execute(stmt):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            result = MagicMock()
            result.all = MagicMock(return_value=[])
            return result

        db = MagicMock()
        db.execute = AsyncMock(side_effect=_execute)
        await _collect_module_rows(db, [uuid.uuid4()], 42)

        sql = seen["sql"]
        assert "module-test" in sql, sql
        assert "42" in sql, sql
