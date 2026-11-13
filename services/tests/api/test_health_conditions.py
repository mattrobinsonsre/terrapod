"""Tests for _compute_health_conditions and workspace JSON serialization."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.execution_mode = overrides.get("execution_mode", "local")
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.engine_version = "1.11"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.resource_cpu = "1"
    ws.parallelism = 10
    ws.configure_parallelism = None
    ws.resource_memory = "2Gi"
    ws.ee_resource_cpu = ""
    ws.ee_resource_memory = ""
    _pool = overrides.get("agent_pool_id", None)
    ws.agent_pool_links = (
        [SimpleNamespace(agent_pool_id=_pool, ordinal=0, agent_pool=None)] if _pool else []
    )
    ws.agent_pool = None
    ws.vcs_connection_id = overrides.get("vcs_connection_id", None)
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.drift_ignore_rules = []
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.plan_expiry_seconds = None
    ws.drift_last_checked_at = None
    ws.drift_status = overrides.get("drift_status", "")
    ws.state_diverged = overrides.get("state_diverged", False)
    ws.vcs_last_polled_at = overrides.get("vcs_last_polled_at", None)
    ws.vcs_last_attempted_at = overrides.get("vcs_last_attempted_at", None)
    ws.vcs_last_error = overrides.get("vcs_last_error", None)
    ws.vcs_last_error_at = overrides.get("vcs_last_error_at", None)
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.created_at = None
    ws.updated_at = None
    return ws


class TestComputeHealthConditions:
    def test_empty_when_no_issues(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace()
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_state_diverged(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(state_diverged=True)
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "state_diverged"
        assert conditions[0]["severity"] == "error"

    def test_no_agent_pool_agent_mode(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(execution_mode="agent", agent_pool_id=None)
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "no_agent_pool"
        assert conditions[0]["severity"] == "warning"

    def test_agent_mode_with_a_pool_has_no_condition(self):
        """The positive case — the one that catches a fixture going stale.

        Without it, a fixture that silently stopped assigning a pool would
        leave every `no_agent_pool` assertion passing for the wrong reason.
        """
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(execution_mode="agent", agent_pool_id=uuid.uuid4())
        assert _compute_health_conditions(ws) == []

    def test_no_agent_pool_local_mode_no_condition(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(execution_mode="local", agent_pool_id=None)
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_vcs_error(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(vcs_last_error="Token expired")
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "vcs_error"
        assert conditions[0]["severity"] == "error"
        assert "Token expired" in conditions[0]["detail"]

    def test_drifted(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="drifted")
        ws.drift_detection_enabled = True
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "drifted"
        assert conditions[0]["severity"] == "warning"

    def test_drifted_hidden_when_detection_disabled(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="drifted")
        ws.drift_detection_enabled = False
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_drift_errored(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="errored")
        ws.drift_detection_enabled = True
        conditions = _compute_health_conditions(ws)
        assert len(conditions) == 1
        assert conditions[0]["code"] == "drift_errored"
        assert conditions[0]["severity"] == "warning"

    def test_drift_errored_hidden_when_detection_disabled(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="errored")
        ws.drift_detection_enabled = False
        conditions = _compute_health_conditions(ws)
        assert conditions == []

    def test_multiple_conditions_simultaneously(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(
            state_diverged=True,
            vcs_last_error="403 Forbidden",
            drift_status="drifted",
            execution_mode="agent",
            agent_pool_id=None,
        )
        ws.drift_detection_enabled = True
        conditions = _compute_health_conditions(ws)
        codes = {c["code"] for c in conditions}
        assert codes == {"state_diverged", "no_agent_pool", "vcs_error", "drifted"}

    def test_workspace_json_includes_conditions(self):
        from terrapod.api.routers.tfe_v2 import _workspace_json

        ws = _mock_workspace(state_diverged=True)
        result = _workspace_json(ws, tfe=False)
        attrs = result["data"]["attributes"]
        assert "health-conditions" in attrs
        assert len(attrs["health-conditions"]) == 1
        assert attrs["health-conditions"][0]["code"] == "state_diverged"

    def test_workspace_json_includes_vcs_fields(self):
        from terrapod.api.routers.tfe_v2 import _workspace_json

        ws = _mock_workspace(vcs_last_error="Something broke")
        result = _workspace_json(ws, tfe=False)
        attrs = result["data"]["attributes"]
        assert "vcs-last-polled-at" in attrs
        assert attrs["vcs-last-error"] == "Something broke"
        assert "vcs-last-error-at" in attrs


def _mock_run(status="errored", error_message=""):
    """The newest primary run, as `_workspace_json`'s callers already load it."""
    run = MagicMock()
    run.status = status
    run.error_message = error_message
    return run


class TestTheLatestRunIsAHealthCondition:
    """A failed run is a health condition (reported 2026-11-13).

    Every condition above derives from a persistent workspace column, and none
    of them read a run -- so a workspace whose last plan or apply FAILED
    reported nothing at all. The status column said `errored` while the Health
    count said the estate was fine, and the Health filter, which unions one
    `flag:` term per condition, could not find it.

    Incoherent rather than merely incomplete: `drift_errored` is itself a failed
    run, since drift detection is just a kind of run, so "a run failed" already
    counted for exactly one kind of run. And `_primary_run_filter` -- which
    decides what `latest_run` is -- says in its own docstring that it exists to
    exclude runs "that should not affect workspace health".
    """

    def test_an_errored_latest_run_is_a_condition(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        conditions = _compute_health_conditions(_mock_workspace(), None, _mock_run())
        codes = [c["code"] for c in conditions]
        assert codes == ["run_errored"], codes
        assert conditions[0]["severity"] == "error", conditions[0]

    def test_the_detail_carries_the_runs_own_message(self):
        """Following `vcs_error`, which puts the real error in `detail`.

        An operator opening the Health Issues expansion wants to know WHAT
        failed; "check the run output" sends them to a page to read something
        this response already has.
        """
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        conditions = _compute_health_conditions(
            _mock_workspace(), None, _mock_run(error_message="Failed to resolve provider packages")
        )
        assert conditions[0]["detail"] == "Failed to resolve provider packages", conditions[0]

    def test_a_run_with_no_message_still_says_something_useful(self):
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        conditions = _compute_health_conditions(_mock_workspace(), None, _mock_run())
        assert "most recent run failed" in conditions[0]["detail"], conditions[0]

    def test_a_workspace_that_has_never_run_has_no_condition(self):
        """`latest_run=None` is a workspace with no primary run, not a failure."""
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        assert _compute_health_conditions(_mock_workspace(), None, None) == []

    def test_a_successful_latest_run_has_no_condition(self):
        """The control. Without it the tests above pass on a condition that
        fires for every workspace that has ever run."""
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        for status in ("applied", "planned", "planning", "applying", "pending", "queued"):
            got = _compute_health_conditions(_mock_workspace(), None, _mock_run(status=status))
            assert got == [], f"{status} produced {got}"

    def test_a_canceled_or_discarded_run_is_not_a_failure(self):
        """A deliberate human act, not an unhealthy workspace.

        Kept separate from the control above because these two are the ones
        somebody would plausibly add to the condition, and they should not be:
        discarding a plan is the normal way to decline one.
        """
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        for status in ("canceled", "discarded"):
            got = _compute_health_conditions(_mock_workspace(), None, _mock_run(status=status))
            assert got == [], f"{status} produced {got}"

    def test_it_reports_alongside_drift_errored_rather_than_instead_of_it(self):
        """Both are true of a drift run that failed, so both are reported.

        They are not duplicates: `drift_errored` reads `drift_status`, a column
        written by a separate triggered handler, and this reads the run. When
        that handler does not run -- which is how this was found, one workspace
        updating and its sibling in the same cycle not -- only this one fires.
        """
        from terrapod.api.routers.tfe_v2 import _compute_health_conditions

        ws = _mock_workspace(drift_status="errored")
        ws.drift_detection_enabled = True
        codes = [c["code"] for c in _compute_health_conditions(ws, None, _mock_run())]
        assert codes == ["run_errored", "drift_errored"], codes


class TestTheCallSitePassesIt:
    """The condition is wired in, not merely defined.

    `_compute_health_conditions` grew a THIRD parameter with a default, so
    omitting it at the call site compiles, runs, and silently restores the
    defect in full -- every behavioural test above would still pass, because
    they call the function directly.
    """

    def test_workspace_json_passes_the_latest_run_to_the_conditions(self):
        import ast
        import inspect

        from terrapod.api.routers import tfe_v2

        tree = ast.parse(inspect.getsource(tfe_v2._workspace_json))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_compute_health_conditions"
        ]
        assert calls, "_workspace_json no longer computes health conditions at all"
        for call in calls:
            passed = [a.id for a in call.args if isinstance(a, ast.Name)] + [
                k.value.id for k in call.keywords if isinstance(k.value, ast.Name)
            ]
            assert "latest_run" in passed, (
                "the latest run is not passed, so every run-derived condition is "
                f"silently skipped: {ast.unparse(call)}"
            )

    def test_every_caller_of_workspace_json_passes_it_too(self):
        """One level further out, which is where the real gap was.

        The gate above stops at `_workspace_json`, so it is satisfied while four
        of its ten callers omit `latest_run` -- the PATCH and the three lock
        endpoints. `health-conditions` was a pure function of workspace columns
        until `run_errored` was added, so those responses silently dropped one
        condition and the same workspace reported a different health depending
        on which endpoint answered: saving the settings form made the banner go
        away until the next GET brought it back.

        Create is the one legitimate omission -- a workspace with no runs yet --
        and it is named rather than inferred, so a second omission has to be
        argued for here instead of passing quietly.
        """
        import ast
        import inspect

        from terrapod.api.routers import tfe_v2

        #: Functions that may call `_workspace_json` without a latest run, with
        #: the reason. Each needs one.
        EXEMPT = {
            "_create_workspace_impl": "a workspace being created has no runs",
        }

        src = inspect.getsource(tfe_v2)
        tree = ast.parse(src)
        offenders = []
        seen = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if fn.name in ("_workspace_json",):
                continue
            for call in ast.walk(fn):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_workspace_json"
                ):
                    continue
                seen += 1
                if any(k.arg == "latest_run" for k in call.keywords):
                    continue
                if fn.name in EXEMPT:
                    continue
                offenders.append(f"{fn.name}: {ast.unparse(call)[:90]}")

        assert seen >= 8, (
            f"only {seen} calls to _workspace_json were inspected -- the matcher has "
            "stopped matching, so this gate is passing on nothing"
        )
        assert not offenders, (
            "these callers omit `latest_run`, so `health-conditions` is endpoint-"
            "dependent and `run_errored` disappears from their responses:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_serialised_conditions_include_it_end_to_end(self):
        """Through `_workspace_json` rather than the helper, so the wiring above
        is proven by behaviour as well as by structure."""
        from terrapod.api.routers.tfe_v2 import _workspace_json

        body = _workspace_json(
            _mock_workspace(),
            latest_run=_mock_run(error_message="boom"),
            tfe=False,
        )
        codes = [c["code"] for c in body["data"]["attributes"]["health-conditions"]]
        assert "run_errored" in codes, codes
