"""A refused diagnostic read is reported, so permanent failures stay visible (#2246).

Three reads in `job_manager` exist to explain a run that has already gone wrong
-- the pod's log, a pod's terminated state, a Job's failure reason -- and all
three ended in a bare `except ApiException: return <empty>`. A refusal therefore
produced an empty result and **no record at any level**.

The cost is specific rather than general. `listener.py`'s handler around
`get_pod_logs` carries a comment saying it exists so that permanent failures stay
diagnosable, naming "auth, RBAC, wrong namespace" and the bare `except` it
replaced -- and the inner swallow defeated it, because nothing reached it. The
caller got `""`, hit `if not logs: return`, and logged nothing. So a listener with
no permission to read pods in its namespace was indistinguishable from a pod that
had not started yet, permanently.

**The quiet half is what makes the loud half worth anything**, which is why every
case below is paired: 404 and 400 are the ordinary shape of "not yet" on this
timeline and must stay at debug, or an operator learns to ignore the log. A test
that only asserted the loud case would pass against a module that shouted on
every launch.

`get_pod_logs` had ZERO test references anywhere under `services/tests/` before
this, against 28 for `get_job_status` in the same module -- a gap rather than a
module nobody tests.
"""

from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from terrapod.runner import job_manager
from terrapod.runner.job_manager import (
    get_job_failure_info,
    get_pod_logs,
    get_pod_terminated_info,
)

_POD = SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="tprun-x-plan-abc"))])


def _core(*, list_exc=None, log_exc=None) -> MagicMock:
    """A core API that answers the pod list and refuses whichever call is named.

    Both calls sit inside ONE `try` in `get_pod_logs`, so a refusal of either is
    swallowed identically -- which is why the list half gets its own case below.
    """
    api = MagicMock()
    if list_exc is not None:
        api.list_namespaced_pod.side_effect = list_exc
    else:
        api.list_namespaced_pod.return_value = _POD
    if log_exc is not None:
        api.read_namespaced_pod_log.side_effect = log_exc
    return api


def _levels(log: MagicMock) -> dict[str, int]:
    return {
        "error": log.error.call_count,
        "warning": log.warning.call_count,
        "debug": log.debug.call_count,
    }


class TestAPermanentRefusalIsReportedLoudly:
    """401 and 403 are never "not yet": no amount of waiting grants a permission."""

    @pytest.mark.parametrize("status", [401, 403])
    async def test_the_log_read_being_refused_is_an_error_naming_the_status(self, status):
        core = _core(log_exc=ApiException(status=status))
        with (
            patch.object(job_manager, "_get_core_api", return_value=core),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            out = await get_pod_logs("tprun-x-plan")

        # Control flow is deliberately unchanged -- the fix is the record, not
        # the fallback, and this read has always been best-effort.
        assert out == ""

        assert _levels(log) == {"error": 1, "warning": 0, "debug": 0}
        _, kwargs = log.error.call_args
        assert kwargs["status"] == status
        assert kwargs["job"] == "tprun-x-plan"
        assert kwargs["namespace"] == "terrapod"
        # **The exact string, because `docs/runbooks.md` quotes it.** A doc that
        # reproduces an error message goes stale in silence -- an operator handed
        # the real text searches the page for it and finds nothing, which is worse
        # than no example at all. Pinning it here is what couples the page to the
        # code, and it costs one assertion.
        #
        # It also has to say the condition will not resolve, because the whole
        # defect was that a permanent failure looked transient for ever.
        message = log.error.call_args[0][0]
        assert message == (
            "Kubernetes refused a diagnostic read; this will not resolve on its own "
            "-- check the listener's RBAC and namespace"
        ), "docs/runbooks.md quotes this message verbatim; update both or neither"

    async def test_the_POD_LIST_being_refused_is_reported_too(self):
        """The first of the two calls, which is the one an RBAC gap hits first.

        Both are inside one `try`, so a test that only refused the log read would
        pass against a handler that reported only the second.
        """
        core = _core(list_exc=ApiException(status=403))
        with (
            patch.object(job_manager, "_get_core_api", return_value=core),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            assert await get_pod_logs("tprun-x-plan") == ""
        assert log.error.call_count == 1


class TestTheOrdinaryNotYetStaysQuiet:
    """The half that makes the loud half meaningful."""

    @pytest.mark.parametrize(
        ("status", "why"),
        [
            (404, "the pod does not exist: not created yet, or already collected"),
            (400, "what the API answers while a container is ContainerCreating"),
        ],
    )
    async def test_a_transient_status_is_debug_only(self, status, why):
        core = _core(log_exc=ApiException(status=status))
        with (
            patch.object(job_manager, "_get_core_api", return_value=core),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            assert await get_pod_logs("tprun-x-plan") == ""
        assert _levels(log) == {"error": 0, "warning": 0, "debug": 1}, why

    async def test_an_unexpected_status_is_a_warning_rather_than_silence(self):
        """5xx may pass, so it is not an error -- but it is not "not yet" either."""
        core = _core(log_exc=ApiException(status=500))
        with (
            patch.object(job_manager, "_get_core_api", return_value=core),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            assert await get_pod_logs("tprun-x-plan") == ""
        assert _levels(log) == {"error": 0, "warning": 1, "debug": 0}


class TestTheTwoSiblingReadsGetTheSameTreatment:
    """Listed in the issue so the sweep is not left half-done.

    Both are reached only when a run has already failed, so a silent refusal
    there costs exactly the explanation somebody went looking for.
    """

    async def test_the_terminated_state_read(self):
        core = _core(list_exc=ApiException(status=403))
        with (
            patch.object(job_manager, "_get_core_api", return_value=core),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            assert await get_pod_terminated_info("tprun-x-plan") is None
        assert log.error.call_count == 1

    async def test_the_job_failure_read(self):
        batch = MagicMock()
        batch.read_namespaced_job.side_effect = ApiException(status=403)
        with (
            patch.object(job_manager, "_get_batch_api", return_value=batch),
            patch.object(job_manager, "_default_namespace", return_value="terrapod"),
            patch.object(job_manager, "logger") as log,
        ):
            assert await get_job_failure_info("tprun-x-plan") is None
        assert log.error.call_count == 1


class TestTheSweepCannotBeHalfUndone:
    """A bare `except ApiException:` anywhere in the module fails here.

    Derived from the source rather than from a list of the three sites, because a
    list is what goes stale: a fourth diagnostic read added later would otherwise
    reintroduce the defect with every test above green. That is the shape this
    repository keeps meeting -- a gate written for the instances it was born with.
    """

    def test_no_bare_api_exception_handler_remains(self):
        source = pathlib.Path(job_manager.__file__).read_text()
        bare = [
            (n, line)
            for n, line in enumerate(source.split("\n"), 1)
            if line.strip() == "except ApiException:"
        ]
        assert not bare, (
            "a bare `except ApiException:` returns an empty result with no record, "
            f"which is #2246: {bare}. Bind the exception and call `_log_api_refusal`."
        )

    def test_the_producer_is_used_by_every_handler_that_binds_one(self):
        """And the converse: binding it is not the same as reporting it."""
        source = pathlib.Path(job_manager.__file__).read_text()
        binds = source.count("except ApiException as exc:")
        reports = source.count("_log_api_refusal(")
        # One definition plus one call per handler.
        assert binds >= 3, f"only {binds} handlers bind the exception; the walk may be blind"
        assert reports >= binds + 1, (
            f"{binds} handlers bind an ApiException but only {reports - 1} report it"
        )


class TestTheSingleContainerAssumptionStillHolds:
    """`get_pod_logs` passes no `container`, and that is only safe while there is one.

    This issue was originally raised claiming the opposite -- that the read must
    name `container="runner"` or the API refuses every configure pod. It was
    false: `ee` is a NATIVE sidecar, an `initContainers` entry with
    `restartPolicy: Always`, so `pod.spec.containers` holds exactly one entry and
    the API defaults the log container. Measured against a pod of that exact shape
    before the issue was re-scoped.

    The assumption is worth a gate rather than a note, because the day a second
    REGULAR container is added the original claim becomes true and every log read
    starts failing with `choose one of: [...]`. Derived over every golden scenario,
    so it cannot be satisfied by the one fixture somebody happened to write.
    """

    def test_no_golden_scenario_has_more_than_one_regular_container(self):
        golden = json.loads((pathlib.Path(__file__).parent / "job_spec_golden.json").read_text())
        assert len(golden) >= 10, f"only {len(golden)} scenarios read; the golden moved"

        offenders = {}
        sidecars = 0
        for name, spec in golden.items():
            pod = ((spec.get("spec") or {}).get("template") or {}).get("spec") or {}
            regular = [c.get("name") for c in (pod.get("containers") or [])]
            if len(regular) != 1:
                offenders[name] = regular
            sidecars += sum(
                1 for c in (pod.get("initContainers") or []) if c.get("restartPolicy") == "Always"
            )

        assert not offenders, (
            "these scenarios have more than one regular container, so a pod-log read "
            f"with no `container` is now ambiguous: {offenders}. Pass `container=` in "
            "`get_pod_logs` -- which is what #2246 originally, wrongly, claimed was "
            "already needed."
        )
        # And the sidecar really is an init entry somewhere, so this is not passing
        # because the EE stopped being configured at all.
        assert sidecars >= 1, "no native sidecar found in any scenario; is the EE still wired?"

    def test_get_pod_logs_passes_no_container(self):
        # Sliced by AST rather than by searching for the next `async def`: this is
        # the LAST function in the module, so a text scan has nothing to stop at
        # and raises -- which is how the first version of this test failed.
        source = pathlib.Path(job_manager.__file__).read_text()
        tree = ast.parse(source)
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
                and n.name == "get_pod_logs"
            ),
            None,
        )
        assert fn is not None, "get_pod_logs is gone; this gate needs re-pointing"

        names = {
            kw.arg
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg
        }
        literals = {
            node.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "container" not in names and "container" not in literals, (
            "get_pod_logs now names a container; if that is deliberate, the golden "
            "gate above is the thing to update"
        )
