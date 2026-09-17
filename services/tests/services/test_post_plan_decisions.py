"""A run stopped after its plan, in the Terraform Enterprise vocabulary (#1704).

The `tofu`/`terraform` CLI decides what to do after a plan from the run's
status and its policy checks and task stages: it prints them, and offers an
override only while the run is `policy_override` or
`post_plan_awaiting_decision`. Terrapod reports that vocabulary when
`runs.tfe_post_plan_decisions` is on or the client asks for it, and otherwise
keeps the 1.x `planning` + `blocked-by` answer.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api import post_plan_decisions
from terrapod.api.routers import run_tasks as run_tasks_router
from terrapod.services import policy_check_service, run_service

FINISHED = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


def _run(**kw):
    run = MagicMock()
    run.id = kw.get("id", uuid.uuid4())
    run.workspace_id = uuid.uuid4()
    run.status = kw.get("status", "planning")
    run.plan_finished_at = kw.get("plan_finished_at", FINISHED)
    run.plan_started_at = FINISHED
    run.plan_only = kw.get("plan_only", False)
    return run


def _request(header: str | None = None):
    request = MagicMock()
    request.headers = {} if header is None else {post_plan_decisions.HEADER: header}
    return request


class TestWhoGetsTheTfeVocabulary:
    @pytest.mark.parametrize(
        ("configured", "header", "expected"),
        [
            (False, None, False),
            (True, None, True),
            (False, "tfe", True),
            (True, "legacy", False),
            (False, " TFE ", True),
            (True, "something-else", True),
        ],
    )
    def test_the_header_wins_and_the_config_is_the_default(self, configured, header, expected):
        with patch.object(post_plan_decisions.settings.runs, "tfe_post_plan_decisions", configured):
            assert post_plan_decisions.reports_tfe_post_plan(_request(header)) is expected

    def test_without_a_request_the_config_decides(self):
        with patch.object(post_plan_decisions.settings.runs, "tfe_post_plan_decisions", True):
            assert post_plan_decisions.reports_tfe_post_plan(None) is True

    def test_it_defaults_off_until_2_0(self):
        from terrapod.config import RunsConfig

        assert RunsConfig().tfe_post_plan_decisions is False


class TestTheTfeStatusOfAHeldRun:
    async def _hold(self, run, *, stage_status=None, policy=False, scan=False):
        stage = None if stage_status is None else MagicMock(status=stage_status)
        with (
            patch(
                "terrapod.services.run_task_service._existing_stage",
                AsyncMock(return_value=stage),
            ),
            patch(
                "terrapod.services.policy_set_service.run_is_policy_blocked",
                AsyncMock(return_value=policy),
            ),
            patch(
                "terrapod.services.security_scan_service.run_is_scan_blocked",
                AsyncMock(return_value=scan),
            ),
        ):
            return await run_service.post_plan_hold(AsyncMock(), run)

    @pytest.mark.parametrize("stage_status", ["pending", "running"])
    async def test_tasks_still_running_are_post_plan_running(self, stage_status):
        hold = await self._hold(_run(), stage_status=stage_status, policy=True)
        assert hold == run_service.PostPlanHold("run-task", "post_plan_running")

    async def test_a_failed_mandatory_task_awaits_a_decision(self):
        hold = await self._hold(_run(), stage_status="failed", policy=True)
        assert hold == run_service.PostPlanHold("run-task", "post_plan_awaiting_decision")

    async def test_a_mandatory_policy_failure_is_policy_override(self):
        hold = await self._hold(_run(), stage_status="passed", policy=True, scan=True)
        assert hold == run_service.PostPlanHold("policy", "policy_override")

    async def test_an_enforced_scan_failure_is_policy_override_too(self):
        # The CLI knows no separate status for a scan; it is served as a
        # policy check, so it holds the run the way a policy does.
        hold = await self._hold(_run(), stage_status="overridden", scan=True)
        assert hold == run_service.PostPlanHold("security-scan", "policy_override")

    async def test_nothing_holds_a_run_whose_plan_is_running(self):
        assert await self._hold(_run(plan_finished_at=None), policy=True) is None

    async def test_blocked_by_is_still_the_gate(self):
        with patch.object(
            run_service,
            "post_plan_hold",
            AsyncMock(return_value=run_service.PostPlanHold("policy", "policy_override")),
        ):
            assert await run_service.blocked_by(AsyncMock(), _run()) == "policy"

    def test_the_statuses_are_never_stored(self):
        # They are only reported: no transition may lead to one.
        targets = set().union(*run_service.VALID_TRANSITIONS.values())
        assert not targets & run_service.TFE_POST_PLAN_STATUSES
        assert not set(run_service.VALID_TRANSITIONS) & run_service.TFE_POST_PLAN_STATUSES


class TestAFailedMandatoryRunTask:
    async def _complete(self, run, *, tfe: bool):
        stage = MagicMock(id=uuid.uuid4())
        with (
            patch("terrapod.config.settings.runs.tfe_post_plan_decisions", tfe),
            patch(
                "terrapod.services.run_task_service.create_task_stage",
                AsyncMock(return_value=stage),
            ),
            patch(
                "terrapod.services.run_task_service.resolve_stage",
                AsyncMock(return_value="failed"),
            ),
            patch.object(run_service, "transition_run", AsyncMock()) as transition,
        ):
            await run_service.complete_plan(AsyncMock(), run)
        return transition

    async def test_errors_the_run_in_1_x(self):
        transition = await self._complete(_run(), tfe=False)
        transition.assert_awaited_once()
        assert transition.await_args.args[2] == "errored"

    async def test_holds_the_run_for_a_decision_in_the_tfe_vocabulary(self):
        transition = await self._complete(_run(), tfe=True)
        transition.assert_not_awaited()

    async def test_still_errors_a_plan_only_run_which_has_nothing_to_decide(self):
        transition = await self._complete(_run(plan_only=True), tfe=True)
        assert transition.await_args.args[2] == "errored"


def _eval(**kw):
    e = MagicMock()
    e.policy_set_name = kw.get("name", "baseline")
    e.enforcement_level = kw.get("level", "mandatory")
    e.outcome = kw.get("outcome", "passed")
    e.overridden_by = kw.get("overridden_by")
    e.overridden_at = FINISHED if e.overridden_by else None
    e.created_at = FINISHED
    e.result = kw.get(
        "result",
        {
            "policies": [
                {
                    "policy": "no-open-ssh",
                    "passed": e.outcome == "passed",
                    "violations": [] if e.outcome == "passed" else ["port 22 open to the world"],
                    "warnings": [],
                    "error": None,
                }
            ]
        },
    )
    return e


def _scan(**kw):
    s = MagicMock()
    s.engine = "checkov"
    s.enforcement_level = kw.get("level", "enforced")
    s.severity_threshold = "high"
    s.outcome = kw.get("outcome", "failed")
    s.overridden_by = kw.get("overridden_by")
    s.overridden_at = FINISHED if s.overridden_by else None
    s.created_at = FINISHED
    s.error = None
    s.summary = {"blocking": 1, "total": 2}
    s.findings = [
        {"severity": "low", "rule_id": "CKV_AWS_23", "title": "Describe rules", "resource": "sg"},
        {"severity": "high", "rule_id": "CKV_AWS_24", "title": "Open SSH", "resource": "sg"},
    ]
    return s


class TestPolicyChecks:
    def test_ids_round_trip(self):
        rid = uuid.uuid4()
        for kind in policy_check_service.KINDS:
            assert policy_check_service.parse_check_id(
                policy_check_service.check_id(kind, rid)
            ) == (kind, rid)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "polchk-",
            "polchk-sentinel-01a0af8c-79a0-759c-86e3-3607f5b419e6",
            "polchk-opa-nope",
            "opa-x",
        ],
    )
    def test_a_malformed_id_is_not_a_check(self, value):
        assert policy_check_service.parse_check_id(value) is None

    def test_a_blocking_mandatory_policy_soft_fails_and_says_why(self):
        check = policy_check_service._opa_check(
            uuid.uuid4(), [_eval(outcome="failed"), _eval(name="tags", outcome="passed")]
        )
        assert check.status == "soft_failed"
        assert check.is_overridable
        assert (check.soft_failed, check.passed, check.scope) == (1, 1, "organization")
        assert "deny: port 22 open to the world" in check.output

    def test_an_advisory_failure_passes(self):
        check = policy_check_service._opa_check(
            uuid.uuid4(), [_eval(level="advisory", outcome="failed")]
        )
        assert (check.status, check.advisory_failed, check.is_overridable) == ("passed", 1, False)

    def test_an_overridden_policy_reports_overridden(self):
        check = policy_check_service._opa_check(
            uuid.uuid4(), [_eval(outcome="failed", overridden_by="admin@example.com")]
        )
        assert check.status == "overridden"
        assert not check.is_overridable
        assert "overridden by admin@example.com" in check.output

    def test_an_errored_mandatory_set_can_be_overridden(self):
        # Terrapod lets an admin release a run whose mandatory set errored (for
        # example a runner too old to evaluate it), so the CLI must offer that.
        check = policy_check_service._opa_check(
            uuid.uuid4(), [_eval(outcome="errored", result={"error": "Runner did not evaluate"})]
        )
        assert check.status == "soft_failed"
        assert "error: Runner did not evaluate" in check.output

    def test_an_enforced_scan_failure_soft_fails_worst_first(self):
        check = policy_check_service._scan_check(uuid.uuid4(), _scan())
        assert (check.status, check.scope) == ("soft_failed", "workspace")
        assert check.output.index("CKV_AWS_24") < check.output.index("CKV_AWS_23")

    @pytest.mark.parametrize(
        ("kw", "status"),
        [
            ({"level": "advisory"}, "passed"),
            ({"outcome": "passed"}, "passed"),
            ({"overridden_by": "admin@example.com"}, "overridden"),
        ],
    )
    def test_scan_statuses(self, kw, status):
        assert policy_check_service._scan_check(uuid.uuid4(), _scan(**kw)).status == status

    async def test_a_run_has_a_check_only_for_a_gate_that_recorded_something(self):
        run = _run()
        with (
            patch(
                "terrapod.services.policy_set_service.get_run_evaluations",
                AsyncMock(return_value=[]),
            ),
            patch(
                "terrapod.services.security_scan_service.get_run_scan",
                AsyncMock(return_value=_scan()),
            ),
        ):
            checks = await policy_check_service.list_checks(AsyncMock(), run)
        assert [c.kind for c in checks] == ["scan"]


class TestTaskStagesForTheCli:
    def _stage(self, status):
        ts = MagicMock(status=status, id=uuid.uuid4(), run_id=uuid.uuid4(), stage="post_plan")
        ts.created_at = ts.updated_at = FINISHED
        ts.results = []
        return ts

    def test_a_failed_stage_holding_a_run_awaits_override(self):
        ts = self._stage("failed")
        assert run_tasks_router.tfe_task_stage_status(ts, _run()) == "awaiting_override"
        doc = run_tasks_router.tfe_task_stage_json(ts, _run(), can_override=True)
        assert doc["attributes"]["actions"]["is-overridable"] is True

    def test_a_failed_stage_on_an_errored_run_is_just_failed(self):
        ts = self._stage("failed")
        assert run_tasks_router.tfe_task_stage_status(ts, _run(status="errored")) == "failed"

    def test_an_overridden_stage_has_passed(self):
        # TFE has no `overridden` stage; the CLI rejects statuses it does not know.
        assert run_tasks_router.tfe_task_stage_status(self._stage("overridden"), _run()) == "passed"

    def test_a_result_always_names_an_enforcement_level(self):
        tsr = MagicMock(
            id=uuid.uuid4(),
            task_stage_id=uuid.uuid4(),
            run_task_id=None,
            run_task=None,
            status="failed",
            message="boom",
            started_at=FINISHED,
            finished_at=FINISHED,
            created_at=FINISHED,
        )
        attrs = run_tasks_router.tfe_task_result_json(tsr)["attributes"]
        assert attrs["workspace-task-enforcement-level"]
        assert attrs["status-timestamps"]["failed-at"] == "2026-09-17T12:30:00Z"


class TestTheRunDocument:
    def _json(self, **kw):
        from terrapod.api.routers.runs import _run_json

        run = _run()
        for attr in (
            "vcs_commit_sha",
            "vcs_branch",
            "vcs_pull_request_number",
            "configuration_version_id",
            "created_by",
        ):
            setattr(run, attr, None)
        with patch.object(run_service, "resolve_auto_apply_mode", return_value="never"):
            return _run_json(run, engine="terraform", **kw)["data"]

    def test_1_x_is_unchanged(self):
        doc = self._json(blocked_by="policy")
        assert doc["attributes"]["status"] == "planning"
        assert "data" not in doc["relationships"]["policy-checks"]
        assert "data" not in doc["relationships"]["task-stages"]

    def test_the_tfe_vocabulary_reports_the_hold_and_lists_what_to_read(self):
        doc = self._json(
            blocked_by="policy",
            reported_status="policy_override",
            policy_check_ids=["polchk-opa-x"],
            task_stage_ids=[],
        )
        assert doc["attributes"]["status"] == "policy_override"
        assert doc["attributes"]["blocked-by"] == "policy"
        assert doc["relationships"]["policy-checks"]["data"] == [
            {"id": "polchk-opa-x", "type": "policy-checks"}
        ]
        assert doc["relationships"]["task-stages"]["data"] == []


class TestTheCliCanDecodeTheStages:
    def test_included_resources_form_no_cycle(self):
        # go-tfe decodes `included` by following relationships into it; a cycle
        # (a result pointing at its stage, which lists the result) recursed
        # until `tofu apply` died of a stack overflow on a live stack.
        tsr = MagicMock(
            id=uuid.uuid4(),
            run_task_id=uuid.uuid4(),
            run_task=MagicMock(name="t", url="https://x.invalid", enforcement_level="mandatory"),
            status="failed",
            message="",
            started_at=FINISHED,
            finished_at=FINISHED,
            created_at=FINISHED,
        )
        ts = MagicMock(status="failed", id=uuid.uuid4(), run_id=uuid.uuid4(), stage="post_plan")
        ts.created_at = ts.updated_at = FINISHED
        ts.results = [tsr]
        tsr.task_stage_id = ts.id
        included = [
            run_tasks_router.tfe_task_stage_json(ts, _run(), can_override=True),
            run_tasks_router.tfe_task_result_json(tsr),
        ]
        by_key = {(r["type"], r["id"]): r for r in included}

        def refs(resource):
            for rel in (resource.get("relationships") or {}).values():
                data = rel.get("data")
                for item in data if isinstance(data, list) else [data] if data else []:
                    key = (item["type"], item["id"])
                    if key in by_key:
                        yield key

        def walk(key, seen):
            assert key not in seen, f"cycle through {key}"
            for nxt in refs(by_key[key]):
                walk(nxt, seen | {key})

        for key in by_key:
            walk(key, frozenset())
