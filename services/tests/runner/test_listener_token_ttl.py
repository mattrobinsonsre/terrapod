"""The listener asks for a token that outlives its Job (#2526).

The listener is the only party that knows how long the token has to live,
because it owns the Job's `activeDeadlineSeconds`. It asked for nothing, so the
API's `token_ttl_seconds` (3600) applied to a configure Job allowed
`configure_timeout_minutes` (180) -- and a configure that ran past the hour
finished with a dead token, losing its log, its per-host events and its
resource profile to 401s at exit.

The behavioural half is what reaches the wire. The structural half is the one
that matters more here: the TTL and the Job's deadline must come from ONE read
of `timeout-minutes`, or the token is sized for a deadline the Job does not
have -- which is the defect again, with a second copy of the number instead of
no copy.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import httpx
import pytest

from terrapod.runner.listener import RunnerListener


def _capturing_client(seen: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(
            200, json={"token": "runtok:x", "expires_in": 1, "phase": "configure"}
        )

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def _listener(client: httpx.AsyncClient) -> RunnerListener:
    listener = RunnerListener()
    listener._http_client = client
    listener.identity = SimpleNamespace(
        listener_id="11111111-1111-1111-1111-111111111111",
        name="listener",
        api_url="http://test",
        certificate_pem="",
    )
    return listener


@pytest.mark.asyncio
async def test_a_requested_ttl_reaches_the_wire():
    seen: dict = {}
    async with _capturing_client(seen) as client:
        await _listener(client)._get_runner_token("abc", phase="configure", ttl=11220)
    assert seen["body"]["ttl"] == 11220
    assert seen["body"]["phase"] == "configure"


@pytest.mark.asyncio
async def test_no_ttl_sends_no_ttl_key():
    """Additive in both directions: an API that predates the sizing already
    accepted `ttl`, and sending nothing leaves that API's own default in force
    rather than a null it would have to interpret."""
    seen: dict = {}
    async with _capturing_client(seen) as client:
        await _listener(client)._get_runner_token("abc", phase="plan")
    assert "ttl" not in seen["body"]


class TestTheTokenAndTheJobAreSizedFromOneRead:
    """The call site, which the two tests above cannot see.

    Mutation-checked by reverting `timeout_minutes=timeout_minutes` at the Job
    build to its original `int(attrs.get("timeout-minutes") or 60)`: that is a
    SECOND read, so the token could be sized from one value and the Job built
    with another, and only `test_the_job_build_reuses_the_same_name` fails.
    """

    @staticmethod
    def _tree() -> ast.AST:
        # Dedented: a method's source is indented inside its class, and
        # `ast.parse` refuses that with an IndentationError naming line 1,
        # which reads as the source being malformed.
        return ast.parse(textwrap.dedent(inspect.getsource(RunnerListener._launch_run)))

    @staticmethod
    def _call(tree: ast.AST, attr: str) -> ast.Call:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
            ):
                return node
        raise AssertionError(f"_launch_run no longer calls {attr}")

    def test_the_token_request_names_a_ttl(self):
        call = self._call(self._tree(), "_get_runner_token")
        assert "ttl" in {kw.arg for kw in call.keywords}, (
            "the listener asks for no TTL, so the API's default applies to a Job "
            "whose deadline the listener alone knows"
        )

    def test_that_ttl_is_the_shared_budget_function(self):
        """Follow the link rather than checking the keyword exists: a literal,
        or `self.runner_config.token_ttl_seconds`, would satisfy a presence
        check while reintroducing the defect."""
        call = self._call(self._tree(), "_get_runner_token")
        value = next(kw.value for kw in call.keywords if kw.arg == "ttl")
        assert isinstance(value, ast.Call), f"ttl= is not a call: {ast.dump(value)}"
        name = value.func.id if isinstance(value.func, ast.Name) else None
        assert name == "runner_token_ttl_for_job", f"ttl= is computed by {name!r}"

    def test_it_is_sized_from_the_jobs_own_deadline(self):
        """The first argument is the name assigned from `timeout-minutes`, not
        a constant: sizing from a fixed 60 would leave a configure short by two
        hours, which is the whole bug."""
        call = self._call(self._tree(), "_get_runner_token")
        value = next(kw.value for kw in call.keywords if kw.arg == "ttl")
        first = value.args[0]
        assert isinstance(first, ast.Name), f"not sized from a variable: {ast.dump(first)}"
        assigned = {
            target.id
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and "timeout-minutes" in ast.dump(node.value)
        }
        assert first.id in assigned, (
            f"ttl= is sized from {first.id!r}, which is not assigned from the "
            "run's own `timeout-minutes`"
        )

    def test_the_job_build_reuses_the_same_name(self):
        """One read, two uses. A second `attrs.get("timeout-minutes")` at the
        Job build would let the token and the Job disagree."""
        tree = self._tree()
        call = self._call(tree, "_get_runner_token")
        ttl_arg = next(kw.value for kw in call.keywords if kw.arg == "ttl").args[0]
        build = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(kw.arg == "timeout_minutes" for kw in node.keywords)
            and node is not call
        )
        passed = next(kw.value for kw in build.keywords if kw.arg == "timeout_minutes")
        assert isinstance(passed, ast.Name) and passed.id == ttl_arg.id, (
            "the Job is built from a second read of `timeout-minutes`, so its "
            f"deadline and its token can disagree: {ast.dump(passed)}"
        )

    def test_the_read_comes_before_the_token_request(self):
        """Positional, because the value has to exist by the time it is used --
        the read was originally thirty lines below the token request."""
        tree = self._tree()
        call = self._call(tree, "_get_runner_token")
        reads = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and "timeout-minutes" in ast.dump(node.value)
        ]
        assert reads and min(reads) < call.lineno
