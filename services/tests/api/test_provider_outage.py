"""A VCS provider outage must not present as a Terrapod fault.

During the GitHub incident on 2026-08-17, GitHub answered every REST call with
403 while its rate-limit headers stayed healthy. Queueing a run on a
VCS-connected workspace returned:

    500  {"detail": "Internal server error"}

because `httpx.HTTPStatusError` propagated out of the run-create handler into
the catch-all `Exception` handler. That message is the worst available one: the
operator cannot tell someone else's outage from their own misconfiguration or
from a bug in Terrapod, and the run genuinely was not created.

The trap these tests pin down is that the handler *looks* guarded. It already
raises a clean 422 when it cannot resolve a SHA:

    sha = await _get_branch_sha(...)
    if not sha:
        raise HTTPException(422, "Cannot get branch HEAD SHA")

but the provider call RAISES on an error status rather than returning None, so
that guard never sees an outage and the exception sails past it.
"""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.errors import vcs_unavailable

_BASE = "http://test"


def _github_error(status: int, url: str = "https://api.github.com/repos/o/r/branches/main"):
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{url}'",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _conn(provider="github"):
    class C:
        pass

    c = C()
    c.provider = provider
    return c


class TestTheBackstopHandler:
    """No provider failure anywhere may render as "Internal server error".

    The targeted handlers below are the explanation; this is the net beneath
    them, so a route nobody thought about cannot report someone else's outage
    as ours.
    """

    async def _get(self, exc: Exception):
        app = create_app()

        @app.get("/_boom")
        async def boom():  # pragma: no cover - invoked via the client
            raise exc

        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url=_BASE) as client:
            return await client.get("/_boom")

    async def test_an_upstream_error_status_is_a_bad_gateway_not_a_server_error(self):
        res = await self._get(_github_error(403))
        assert res.status_code == 502
        body = res.json()
        assert "403" in body["detail"]
        assert "api.github.com" in body["detail"]
        assert "Internal server error" not in body["detail"]

    async def test_an_upstream_timeout_is_a_gateway_timeout(self):
        res = await self._get(
            httpx.ReadTimeout("timed out", request=httpx.Request("GET", "https://api.github.com/x"))
        )
        assert res.status_code == 504
        assert "timed out" in res.json()["detail"].lower()

    async def test_an_unreachable_upstream_is_a_bad_gateway(self):
        res = await self._get(
            httpx.ConnectError("refused", request=httpx.Request("GET", "https://api.github.com/x"))
        )
        assert res.status_code == 502

    async def test_a_credential_in_the_url_is_never_echoed_back(self):
        """A redirected archive download can carry a token in the query string,
        and this detail is both returned to the caller and written to the log."""
        exc = _github_error(
            403, "https://codeload.github.com/o/r/tar.gz/abc123?token=SECRETVALUE&x=1"
        )
        res = await self._get(exc)
        assert "SECRETVALUE" not in res.text
        assert "token=" not in res.text
        assert "codeload.github.com" in res.json()["detail"]

    async def test_a_genuine_bug_is_still_a_500(self):
        """The net must not swallow our own faults into a provider-shaped
        excuse — a plain exception is still ours to own."""
        res = await self._get(ValueError("a real bug"))
        assert res.status_code == 500
        assert res.json()["detail"] == "Internal server error"


class TestTheOperatorFacingMessage:
    """What the person who pressed the button actually reads.

    The web UI renders the response's top-level `detail` verbatim
    (`workspaces/[id]/page.tsx`), so this string IS the UX.
    """

    def test_it_names_the_provider_the_repo_and_the_status(self):
        exc = vcs_unavailable(_conn("github"), "acme/infra", "main", _github_error(403))
        assert exc.status_code == 502
        assert "GitHub" in exc.detail, "the provider must be named, not 'the VCS provider'"
        assert "acme/infra@main" in exc.detail
        assert "403" in exc.detail

    def test_it_says_nothing_was_created(self):
        """Otherwise the operator cannot tell whether to retry or whether a
        half-made run is now sitting somewhere."""
        exc = vcs_unavailable(_conn(), "acme/infra", "main", _github_error(502))
        assert "Nothing was created" in exc.detail

    def test_a_missing_repo_stays_the_operators_problem(self):
        """404 is configuration, not an outage: the repo URL is wrong, the
        branch is gone, or the App lost access. Reporting that as 502 would
        send someone to a status page over their own typo."""
        exc = vcs_unavailable(_conn(), "acme/infra", "main", _github_error(404))
        assert exc.status_code == 422
        assert "no acme/infra@main" in exc.detail

    def test_a_rate_limit_403_is_told_apart_from_an_outage_403(self):
        """The distinction that decides what the operator does next.

        GitHub uses 403 for a secondary rate limit AND for being broken. If we
        exhausted the budget the answer is to poll less or split the connection;
        if the provider is simply failing, the answer is to wait. Reusing the
        poller's describer gets this for free — and keeps the sentence identical
        to the one in the workspace's health banner.
        """
        request = httpx.Request("GET", "https://api.github.com/repos/o/r/branches/main")
        limited = httpx.HTTPStatusError(
            "403",
            request=request,
            response=httpx.Response(
                403,
                request=request,
                headers={"x-ratelimit-remaining": "0", "retry-after": "812"},
            ),
        )
        exc = vcs_unavailable(_conn(), "acme/infra", "main", limited)
        assert "rate limit" in exc.detail
        assert "812" in exc.detail, "the operator needs to know how long to wait"

        # The same status with a healthy budget must NOT be blamed on the budget.
        outage = vcs_unavailable(_conn(), "acme/infra", "main", _github_error(403))
        assert "rate limit" not in outage.detail
        assert "403" in outage.detail

    def test_gitlab_is_named_gitlab(self):
        exc = vcs_unavailable(_conn("gitlab"), "grp/proj", "main", _github_error(403))
        assert "GitLab" in exc.detail

    def test_a_timeout_is_distinguished_from_a_refusal(self):
        exc = vcs_unavailable(
            _conn(),
            "acme/infra",
            "main",
            httpx.ReadTimeout("t", request=httpx.Request("GET", "https://api.github.com/x")),
        )
        assert exc.status_code == 504
        assert "timed out" in exc.detail


class TestTheGuardedCallSites:
    """Every route that reaches a provider must translate its failure.

    Asserted structurally rather than by driving each endpoint: the failure
    mode is an *unguarded* call, and a test per endpoint only ever covers the
    endpoints someone remembered.
    """

    def test_no_router_reaches_a_provider_without_translating_its_failure(self):
        import ast
        import pathlib

        import terrapod.api.routers as routers_pkg

        provider_calls = (
            "github_service.",
            "gitlab_service.",
            "_get_branch_sha(",
            "_list_branches(",
            "_list_tags(",
            "_resolve_branch(",
            "_fetch_vcs_config(",
        )
        # A router that hands the failure to a caller which guards it, or that
        # only reaches a provider from a background handler, is fine.
        exempt = {"vcs_events"}

        offenders = []
        for path in sorted(pathlib.Path(routers_pkg.__file__).parent.glob("*.py")):
            if path.stem in exempt:
                continue
            src = path.read_text()
            if not any(c in src for c in provider_calls):
                continue
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                body = ast.get_source_segment(src, node) or ""
                if not any(c in body for c in provider_calls):
                    continue
                if "except" not in body:
                    offenders.append(f"{path.stem}.{node.name}")

        assert not offenders, (
            "these handlers reach a VCS provider with no error translation, so a "
            f"provider outage becomes a bare 500: {offenders}"
        )


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
async def test_every_flavour_of_provider_failure_avoids_a_500(status):
    """401 and 403 included on purpose: GitHub uses 403 for a secondary rate
    limit as well as for permission problems, and this incident returned 403
    from a provider that was simply broken. None of them is a 500."""
    exc = vcs_unavailable(_conn(), "acme/infra", "main", _github_error(status))
    assert exc.status_code in (502, 504)
