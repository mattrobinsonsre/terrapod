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
import json
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
        ("builder", "kwargs", "expected"),
        [
            (
                "run_updated_payload",
                {"workspace_id": "ws-1", "run_id": "run-1", "changed": "plan-json"},
                {"event", "run_id", "workspace_id", "changed"},
            ),
            (
                "log_updated_payload",
                {"workspace_id": "ws-1", "run_id": "run-1", "phase": "configure"},
                {"event", "run_id", "workspace_id", "phase"},
            ),
        ],
    )
    def test_the_event_carries_what_the_page_keys_on(self, builder, kwargs, expected):
        """Asserted by CALLING the producer, not by reading a publisher's source.

        It used to walk `_publish_run_updated`'s own dict literal. That broke the
        moment the shape moved into one producer shared by the inline and deferred
        paths (#2720) — and the source walk would have reported "publishes
        nothing" for a publisher that works perfectly, which is the weaker check
        anyway: this one would catch a key renamed in the builder too.
        """
        from terrapod.services import run_events

        keys = set(json.loads(getattr(run_events, builder)(**kwargs)))
        assert keys == expected, f"{builder} produces {sorted(keys)}, expected {sorted(expected)}"

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


# ---------------------------------------------------------------------------
# One producer per run event, and none of them inline (#2720)
# ---------------------------------------------------------------------------
#
# Everything above derives its subject from `run_artifacts.py` and so had no view
# of `run_service.transition_run`, which is where the run STATUS event is
# published. That is how #2720 shipped: a gate whose name describes the class,
# whose subject was one module, and whose defect was in the other one.
#
# These derive across the whole package instead. Two properties:
#
#   1. each wire shape has ONE producer -- `services/run_events.py`. Two modules
#      building the same payload is how they come to disagree, and the status one
#      and the artifact ones were built in different modules before this.
#   2. the SERVICE layer never publishes inline. An event on the wire before its
#      transaction commits is one a subscriber can read ahead of, and the service
#      layer's callers commit late: the reconciler commits once outside its
#      per-run loop. The router's publishes stay inline on purpose -- an API
#      handler commits as soon as it returns -- which is why this asserts the
#      shape is shared rather than that every publish is deferred.

#: The module allowed to build a run event's payload.
PRODUCER = "terrapod/services/run_events.py"

#: Every wire `event` name that reaches the run detail or workspace page.
RUN_EVENT_NAMES = ("run_status_change", "run_updated", "log_updated")

#: Service-layer modules that emit run events, and must do so deferred.
DEFERRING_MODULES = (
    "terrapod/services/run_service.py",
    "terrapod/services/run_reconciler.py",
    "terrapod/services/ha_role.py",
)

#: Below this the walk has stopped finding emitters and proves nothing.
#:
#: A lower bound is deliberately what is meant here, and the other direction is
#: protected elsewhere: this count GROWING means more events are deferred, which
#: is the desired direction, while a NEW inline producer -- the thing a floor
#: could not see -- is caught by `test_no_second_module_builds_a_run_event_payload`
#: above, which derives its subject from every file in the package. So the only
#: failure this guards is the AST walk matching nothing after a rename.
MIN_DEFERRED_CALLS = 5


def _package_root() -> pathlib.Path:
    return pathlib.Path(run_artifacts.__file__).parents[3]


def _py_files() -> list[pathlib.Path]:
    root = _package_root() / "terrapod"
    return sorted(f for f in root.rglob("*.py") if "__pycache__" not in str(f))


class TestOneProducerPerRunEvent:
    def test_no_second_module_builds_a_run_event_payload(self) -> None:
        root = _package_root()
        offenders: dict[str, list[str]] = {}
        for f in _py_files():
            rel = str(f.relative_to(root))
            if rel == PRODUCER:
                continue
            text = f.read_text()
            hits = [n for n in RUN_EVENT_NAMES if f'"event": "{n}"' in text]
            if hits:
                offenders[rel] = hits
        assert offenders == {}, (
            f"a run event payload is built outside {PRODUCER}: {offenders}. "
            "Two producers of one wire shape is how they come to disagree — use "
            "`run_events.<name>_payload(...)` or `run_events.defer_<name>(...)`."
        )

    def test_the_producer_really_builds_them_all(self) -> None:
        """A floor: a rename must not make the test above pass on nothing."""
        text = (_package_root() / PRODUCER).read_text()
        missing = [n for n in RUN_EVENT_NAMES if f'"event": "{n}"' not in text]
        assert missing == [], f"{PRODUCER} no longer builds: {missing}"


class TestTheServiceLayerNeverPublishesInline:
    def test_every_service_emitter_defers(self) -> None:
        root = _package_root()
        bad: dict[str, int] = {}
        total = 0
        for rel in DEFERRING_MODULES:
            tree = ast.parse((root / rel).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in (
                    "publish_event",
                    "publish_workspace_event",
                ):
                    bad[rel] = bad.get(rel, 0) + 1
                if isinstance(fn, ast.Attribute) and fn.attr.startswith("defer_"):
                    total += 1
        assert bad == {}, (
            f"the service layer publishes a run event inline: {bad}. These modules' "
            "callers commit late — the reconciler commits once outside its per-run "
            "loop — so an inline publish is readable ahead of its own row (#2720)."
        )
        assert total >= MIN_DEFERRED_CALLS, (
            f"only {total} deferred publish call(s) across {len(DEFERRING_MODULES)} "
            f"modules; expected at least {MIN_DEFERRED_CALLS}"
        )
