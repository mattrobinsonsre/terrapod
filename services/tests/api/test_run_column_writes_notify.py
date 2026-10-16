"""A handler that writes a run column must tell the browser (#2119).

The run detail page refetches the run on one event after mount. Three upload
handlers in `run_artifacts.py` wrote columns the page renders and published
nothing, and on the Terraform path two of them land AFTER the `plan-result`
POST that caused the transition the page last refetched on — so the Overview's
Changes and Cost cards stayed empty until the operator reloaded.

The defect is a CLASS, not three instances: `_publish_log_updated` has existed
in that module since #424 for precisely this shape, with a docstring ending
"the trailing bytes from the EXIT trap are invisible until the user hits
Refresh", and it was simply never extended to the handlers that carry the
numbers. A fourth could be added the same way. So this derives its subject from
the source — every route handler that assigns to a `run.*` column — rather than
naming the three, and a new one is in scope automatically.

**It resolves same-module helpers, and that is not incidental.** The plan-json
publish deliberately lives in `_enqueue_plan_json_followups`, beside the pull
request status-comment refresh, because the two exist for the identical reason:
the counts are written by that request, after the transition. A gate walking
only the handler body reads that handler as publishing nothing — which it did
on the first run of this file, before the fix was wrong.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from terrapod.api.routers import run_artifacts
from terrapod.redis import client as redis_client
from terrapod.runner.phases import resource_profile

#: Writes that do not change what any surface renders, so they need no event.
#: Empty today, and present so a future exemption has to state its reason here
#: rather than being achieved by narrowing the derivation above.
WRITES_NOTHING_RENDERED: dict[str, str] = {}

#: Below this, the walk has stopped finding handlers and proves nothing.
MIN_HANDLERS = 5


def _module() -> ast.Module:
    return ast.parse(pathlib.Path(run_artifacts.__file__).read_text())


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def _is_route(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("router" in ast.unparse(d) for d in fn.decorator_list)


def _run_columns_written(fn: ast.AST) -> set[str]:
    """Columns this function assigns via `run.<attr> = ...`."""
    out: set[str] = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if (
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "run"
            ):
                out.add(t.attr)
    return out


def _reaches_a_publish(
    fn: ast.AST,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Whether `fn` publishes, directly or through a same-module helper.

    One module, so the recursion terminates on `seen`; cross-module calls are
    deliberately not followed — a publish reached through another module would
    be a surprising place to put it, and the gate failing is the right outcome.
    """
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        name = ast.unparse(n.func)
        if "_publish_run_updated" in name:
            return True
        target = name.split(".")[-1]
        if target in functions and target not in seen:
            if _reaches_a_publish(functions[target], functions, seen=seen | {target}):
                return True
    return False


def _handlers_writing_run_columns():
    tree = _module()
    functions = _functions(tree)
    found = []
    for name, fn in functions.items():
        if not _is_route(fn):
            continue
        cols = _run_columns_written(fn)
        if cols:
            found.append((name, fn, cols, functions))
    return found


class TestEveryHandlerWritingARunColumnNotifiesTheBrowser:
    def test_the_walk_found_the_handlers_at_all(self):
        tree = _module()
        routes = [n for n in _functions(tree).values() if _is_route(n)]
        assert len(routes) >= MIN_HANDLERS, (
            f"found only {len(routes)} route handlers in run_artifacts.py — this gate has "
            "stopped looking at the thing it is about (decorator renamed?)"
        )

    def test_each_one_publishes_run_updated(self):
        offenders = []
        for name, fn, cols, functions in _handlers_writing_run_columns():
            if name in WRITES_NOTHING_RENDERED:
                continue
            if not _reaches_a_publish(fn, functions):
                offenders.append(f"{name} writes {sorted(cols)} and publishes nothing")
        assert not offenders, (
            f"these write run columns the UI renders without telling it (#2119): {offenders}"
        )

    def test_at_least_the_three_known_handlers_are_in_scope(self):
        """A floor on the derivation, not a list it is driven from.

        If the AST shape stops matching — `run` rebound to another name, the
        writes moved behind a helper — every handler silently drops out of
        scope and the gate above passes by inspecting nothing.
        """
        names = {name for name, _, _, _ in _handlers_writing_run_columns()}
        for expected in (
            "upload_plan_json_output",
            "upload_cost_estimate",
            "record_resource_profile",
        ):
            assert expected in names, (
                f"{expected} no longer reads as writing a run column; the derivation has "
                f"drifted from the code. In scope: {sorted(names)}"
            )


class TestThePayloadIsUsableByTheClient:
    """`changed` is what lets the page refetch one panel rather than guess."""

    @pytest.mark.parametrize(
        ("helper", "expected"),
        [("_publish_run_updated", {"event", "run_id", "workspace_id", "changed"})],
    )
    def test_the_event_carries_what_the_page_keys_on(self, helper, expected):
        fn = _functions(_module())[helper]
        keys = {
            k.value
            for n in ast.walk(fn)
            if isinstance(n, ast.Dict)
            for k in n.keys
            if isinstance(k, ast.Constant)
        }
        assert keys == expected, f"{helper} publishes {sorted(keys)}, expected {sorted(expected)}"

    def test_every_call_site_names_what_changed_with_a_literal(self):
        """A computed `changed` cannot be matched by the page, which compares it
        to a literal. Each site passes a distinct constant."""
        tree = _module()
        values = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and "_publish_run_updated" in ast.unparse(n.func):
                # (workspace_id, run_id, changed)
                if len(n.args) == 3:
                    values.append(n.args[2])
        assert len(values) >= 3, f"found {len(values)} call sites, expected at least 3"
        literals = [v.value for v in values if isinstance(v, ast.Constant)]
        assert len(literals) == len(values), "a `changed` argument is computed, not a literal"
        assert len(set(literals)) == len(literals), (
            f"two call sites report the same `changed` value: {literals}"
        )


class TestAHangingRedisCannotStallAnArtifactUpload:
    """Both helpers' `except Exception` says SSE failures must never break an
    in-flight upload. A hang is not an exception, so the handler could not
    deliver that: `publish_event` awaits `redis.publish` and the client carries
    no `socket_timeout`, so a blackholed Redis stalled the runner's POST.

    Driven rather than asserted about. A shape test on the source would pass
    on a `wait_for` with no timeout, and the property here is about WHEN the
    helper returns.
    """

    @pytest.mark.parametrize(
        "helper,args",
        [
            ("_publish_log_updated", ("ws-1", "run-1", "plan")),
            ("_publish_run_updated", ("ws-1", "run-1", "plan-json")),
        ],
    )
    async def test_the_helper_returns_rather_than_waiting_for_redis(
        self, helper, args, monkeypatch
    ):
        started = asyncio.Event()

        async def _never_returns(*_a, **_k):
            started.set()
            await asyncio.sleep(3600)

        # Patched at the SOURCE module, not on `run_artifacts`: the helper
        # imports `publish_event` inside the function, so the name resolves at
        # call time and a module attribute here would be patching nothing --
        # the test would exercise the real Redis client and pass for the wrong
        # reason, or fail with AttributeError as it first did.
        monkeypatch.setattr(redis_client, "publish_event", _never_returns)
        monkeypatch.setattr(run_artifacts, "_PUBLISH_TIMEOUT_SECONDS", 0.05)

        # The outer bound is the test's own assertion: unbounded, this hangs
        # until pytest is killed, so a generous wait still distinguishes the
        # two outcomes without being a stopwatch on the real timeout.
        await asyncio.wait_for(getattr(run_artifacts, helper)(*args), timeout=10)
        assert started.is_set(), "the publish was never attempted"

    async def test_the_bound_is_a_real_number(self):
        """A `wait_for` whose timeout is None is not a bound, and that is the
        easy wrong fix once someone decides the constant is in the way."""
        assert isinstance(run_artifacts._PUBLISH_TIMEOUT_SECONDS, (int, float))
        assert run_artifacts._PUBLISH_TIMEOUT_SECONDS > 0

    def test_the_bound_is_under_the_tightest_client_deadline(self):
        """DERIVED from the caller, because a bound equal to the deadline it
        protects is not a bound.

        It shipped at 5.0s, which is exactly `resource_profile.post_profile`'s
        own `httpx.Timeout(5.0, ...)` -- single attempt, no retry. So on that
        route the handler could not return before the runner gave up: a
        guaranteed spurious failure and a stall on the Job's exit path, every
        run, from the constant that exists to prevent exactly that. An
        `0 < x <= 30` assertion passed at 5.0 and would pass at 30.
        """
        src = pathlib.Path(resource_profile.__file__).read_text()
        tree = ast.parse(src)
        timeouts = [
            a.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("Timeout")
            for a in n.args
            if isinstance(a, ast.Constant) and isinstance(a.value, (int, float))
        ]
        assert timeouts, "could not read the resource-profile client's timeout"
        assert run_artifacts._PUBLISH_TIMEOUT_SECONDS < min(timeouts), (
            f"the publish bound ({run_artifacts._PUBLISH_TIMEOUT_SECONDS}s) is not "
            f"under the tightest client deadline ({min(timeouts)}s), so that caller "
            "gives up before the handler can return"
        )

    def test_every_publish_in_the_module_is_bounded(self):
        """DERIVED, not a list of two.

        The first version of this class named `_publish_log_updated` and
        `_publish_run_updated`. The pre-release review then found three more
        publishes in the same file -- on the state-upload and state-diverged
        RUNNER endpoints -- which the gate was structurally blind to, because
        it enumerated its subject instead of deriving it. That is the
        ledger-gate shape, and this is its fix.

        The subject is the two Redis PRIMITIVES, not anything whose name
        contains "publish": an earlier draft matched the latter and flagged six
        call sites of the bounded wrappers themselves, which carry the timeout
        inside. A gate can be too wide as well as too narrow, and a wide one
        teaches people to double-bound to silence it.

        And the test is on the CALL node rather than the `await`, because once
        a publish is bounded it is an ARGUMENT of `asyncio.wait_for` and is no
        longer awaited directly -- a draft that walked `ast.Await` found zero
        once the fix was in, and a floor of zero asserts nothing.
        """
        PRIMITIVES = {"publish_event", "publish_workspace_event"}
        tree = ast.parse(pathlib.Path(run_artifacts.__file__).read_text())

        wrapped = {
            id(n.args[0])
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "asyncio.wait_for" and n.args
        }
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in PRIMITIVES
        ]
        # The floor is bound to the module's IMPORTS, which is the only source
        # of truth here that is independent of this gate's own filter.
        #
        # Two earlier floors were worthless and both were caught by running
        # them. `>= 5` was main-specific -- main has three
        # `publish_workspace_event` sites and the release lines two, because
        # main also keeps the break-glass state index current. Comparing the
        # kinds found against `PRIMITIVES` was worse: `calls` is FILTERED by
        # `PRIMITIVES`, so the two agree by construction and narrowing the set
        # left the gate green while making it blind. A check derived from the
        # thing it is checking asserts nothing.
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "terrapod.redis.client"
            for alias in n.names
            if alias.name.startswith("publish")
        }
        assert imported, (
            "the module imports no publish primitive from terrapod.redis.client; this "
            "gate has drifted from the code and is asserting nothing"
        )
        checked = {ast.unparse(n.func).split(".")[-1] for n in calls}
        assert imported <= checked, (
            f"the module imports {sorted(imported)} but this gate only inspects "
            f"{sorted(checked)}; {sorted(imported - checked)} is published somewhere "
            "it is not watching"
        )
        unbounded = [f"line {n.lineno}: {ast.unparse(n)}" for n in calls if id(n) not in wrapped]
        assert not unbounded, (
            "publish(es) with no timeout -- `publish_event` and "
            "`publish_workspace_event` swallow a failure but cannot swallow a hang, "
            "so these stall the request they ride on:\n  " + "\n  ".join(unbounded)
        )
