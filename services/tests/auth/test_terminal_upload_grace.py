"""A runner may finish uploading its own log after its run goes terminal (#2564).

The runner writes the combined phase log and the resource profile at EXIT, after
`LogCapture` has closed its handle -- which is necessarily after the result POST
that moved the run to `applied`. So those two uploads always arrived at a run the
revocation of GHSA-xmrf-hxq9-m59m had already refused, and every successful apply
lost its stored log: the run page showed the last live snapshot until that expired
and then showed nothing at all. A failed phase lost its log the same way, and that
is the log most worth having.

The reprieve is deliberately three-fold -- a writing method, one of the routes the
runner uses at exit, and a run that went terminal within the window -- so what it
widens is "may finish writing its own log", never "may act". Each of the three is
tested as a refusal in its own right, because any one of them alone would be a
general reprieve wearing a narrow name.
"""

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.auth import runner_token_state as rts

_KEY = "tp:runtok_state:{}"


class _FakeRedis:
    def __init__(self, initial=None):
        self.store: dict[str, str] = dict(initial or {})
        self.sets: list[tuple[str, str, int | None]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.sets.append((key, value, ex))


def _redis(initial=None):
    return patch("terrapod.redis.client.get_redis_client", return_value=_FakeRedis(initial))


def _stamped(age_seconds: float) -> str:
    return f"revoked:{int(time.time() - age_seconds)}"


class TestTheRouteListIsDerivedFromWhatTheRunnerUploads:
    """A new exit-time upload must not start silently 401ing the way these did.

    The set is checked against the runner's own two upload modules rather than
    against a list written here, because a list is a claim about the runner that
    nothing keeps in step with it.
    """

    def test_every_phase_log_route_is_covered(self):
        from terrapod.auth.runner_tokens import RUNNER_PHASES

        expected = {f"/artifacts/{phase}-log" for phase in RUNNER_PHASES}
        missing = expected - set(rts.GRACE_PATH_SUFFIXES)
        assert not missing, (
            f"a runner phase whose log upload would be refused at exit: {sorted(missing)}. "
            "Add its suffix to GRACE_PATH_SUFFIXES."
        )

    def test_the_set_is_exactly_the_exit_time_uploads(self):
        from terrapod.auth.runner_tokens import RUNNER_PHASES

        expected = {f"/artifacts/{phase}-log" for phase in RUNNER_PHASES} | {"/resource-profile"}
        assert set(rts.GRACE_PATH_SUFFIXES) == expected, (
            "GRACE_PATH_SUFFIXES has drifted from the runner's exit path. It is a "
            "credential reprieve, so an entry that is not an exit-time upload is a "
            "widening, and a missing one is the #2564 defect returning."
        )

    def test_the_two_upload_modules_build_urls_this_set_matches(self):
        """The suffixes are matched against a URL the runner constructs, so the
        construction is what they have to agree with -- not a route table."""
        import pathlib

        import terrapod.runner.phases.log_capture as log_capture
        import terrapod.runner.phases.resource_profile as resource_profile

        sources = [
            pathlib.Path(log_capture.__file__).read_text(),
            pathlib.Path(resource_profile.__file__).read_text(),
        ]
        joined = "\n".join(sources)
        assert "/artifacts/{phase}-log" in joined, (
            "the phase-log upload no longer builds its URL this way; re-derive "
            "GRACE_PATH_SUFFIXES from however it builds it now"
        )
        assert "/resource-profile" in joined

    def test_the_window_is_long_enough_for_the_upload_to_retry(self):
        """`log_capture` retries on a linear backoff. A window shorter than the
        ladder would let the first attempt through and refuse the retry, which is
        the same defect for an upload that needed one."""
        assert rts.TERMINAL_UPLOAD_GRACE_SECONDS >= 60


class TestPathEligibility:
    @pytest.mark.parametrize("suffix", sorted(rts.GRACE_PATH_SUFFIXES))
    @pytest.mark.parametrize("prefix", ["/api/v1", "/api/terrapod/v1"])
    def test_each_route_is_eligible_on_both_native_prefixes(self, prefix, suffix):
        """Our own runner images still construct the deprecated prefix, so a
        matcher that knew only the canonical one would fix nothing."""
        assert rts.path_is_grace_eligible(f"{prefix}/runs/{uuid.uuid4()}{suffix}") is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/runs/abc/artifacts/state",
            "/api/v1/runs/abc/artifacts/plan-json",
            "/api/v1/runs/abc/apply-result",
            "/api/v1/runs/abc",
            "/api/v1/workspaces/abc/state-versions",
            "/api/v1/runs/abc/artifacts/plan-log/extra",
        ],
    )
    def test_everything_else_is_refused(self, path):
        assert rts.path_is_grace_eligible(path) is False


class TestTheGraceWindow:
    async def test_a_run_that_just_ended_is_inside_it(self):
        run_id = str(uuid.uuid4())
        with _redis({_KEY.format(run_id): _stamped(1)}):
            assert await rts.terminal_upload_grace_active(run_id) is True

    async def test_a_run_that_ended_long_ago_is_outside_it(self):
        run_id = str(uuid.uuid4())
        with _redis({_KEY.format(run_id): _stamped(rts.TERMINAL_UPLOAD_GRACE_SECONDS + 5)}):
            assert await rts.terminal_upload_grace_active(run_id) is False

    async def test_an_unstamped_marker_grants_nothing(self):
        """Two ways a bare marker arises, and neither may grant a window: a
        replica that predates this writes one, and so does the self-healing write
        on the row-check path, which fires when a request arrives rather than when
        the run ended."""
        run_id = str(uuid.uuid4())
        with _redis({_KEY.format(run_id): "revoked"}):
            assert await rts.terminal_upload_grace_active(run_id) is False

    @pytest.mark.parametrize("value", ["revoked:", "revoked:soon", "active", "", "revoked:1:2"])
    async def test_an_unparseable_marker_grants_nothing(self, value):
        run_id = str(uuid.uuid4())
        with _redis({_KEY.format(run_id): value}):
            assert await rts.terminal_upload_grace_active(run_id) is False

    async def test_a_missing_marker_grants_nothing(self):
        with _redis():
            assert await rts.terminal_upload_grace_active(str(uuid.uuid4())) is False

    async def test_a_stamp_in_the_future_grants_nothing(self):
        """Clock skew between replicas must not open the window wider than it is."""
        run_id = str(uuid.uuid4())
        with _redis({_KEY.format(run_id): f"revoked:{int(time.time()) + 600}"}):
            assert await rts.terminal_upload_grace_active(run_id) is False

    async def test_redis_being_away_grants_nothing(self):
        """The ordinary auth path fails OPEN on an infrastructure error, which is
        right for a live run. This one fails CLOSED: it is a reprieve for a run
        already known to be terminal, so an unanswerable question is a refusal."""
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=RuntimeError("redis is away"),
        ):
            assert await rts.terminal_upload_grace_active(str(uuid.uuid4())) is False


class TestRevocationStampsTheMarker:
    async def test_the_transition_records_when_it_happened(self):
        run_id = uuid.uuid4()
        redis = _FakeRedis()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await rts.revoke_run_tokens(run_id)
        head, _, stamp = redis.store[_KEY.format(run_id)].partition(":")
        assert head == "revoked"
        assert abs(int(stamp) - time.time()) < 5

    async def test_a_stamped_marker_still_refuses_the_token_itself(self):
        """The stamp is additional information, not a weakening: everything that
        reads the marker to refuse a token must go on refusing it."""
        run_id = str(uuid.uuid4())
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = "planning"  # would allow, if read
        db.execute = AsyncMock(return_value=result)
        with _redis({_KEY.format(run_id): _stamped(1)}):
            assert await rts.is_run_token_usable(run_id, db) is False
        db.execute.assert_not_awaited()

    async def test_the_row_check_does_not_open_a_window_for_an_old_run(self):
        """The self-healing write on the row-check path must stay unstamped.

        That write fires when a request arrives, not when the run ended, so
        stamping it would hand an hour-old token a fresh 120-second window to
        overwrite that run's stored log merely by presenting itself. The row says
        a run is terminal; it does not say when it became so.
        """
        run_id = str(uuid.uuid4())
        redis = _FakeRedis()
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = "applied"
        db.execute = AsyncMock(return_value=result)
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await rts.is_run_token_usable(run_id, db) is False
            assert await rts.terminal_upload_grace_active(run_id) is False
        assert redis.store[_KEY.format(run_id)] == "revoked"


class TestTheThreeConditionsAtTheAuthBoundary:
    """`_end_of_phase_upload_allowed` is the whole of the widening, so each of its
    three conditions is asserted as a refusal on its own."""

    @staticmethod
    def _request(method: str, path: str):
        req = MagicMock()
        req.method = method
        req.url = MagicMock()
        req.url.path = path
        return req

    async def test_a_writing_method_on_an_exit_route_inside_the_window_is_allowed(self):
        from terrapod.api.dependencies import _end_of_phase_upload_allowed

        run_id = str(uuid.uuid4())
        req = self._request("PUT", f"/api/terrapod/v1/runs/{run_id}/artifacts/apply-log")
        with _redis({_KEY.format(run_id): _stamped(1)}):
            assert await _end_of_phase_upload_allowed(req, run_id) is True

    @pytest.mark.parametrize("method", ["GET", "DELETE", "PATCH", "HEAD"])
    async def test_a_method_that_does_not_write_is_refused(self, method):
        """Reading a finished run's log is an ordinary authenticated action; a
        revoked runner token must not become a way to do it."""
        from terrapod.api.dependencies import _end_of_phase_upload_allowed

        run_id = str(uuid.uuid4())
        req = self._request(method, f"/api/terrapod/v1/runs/{run_id}/artifacts/apply-log")
        with _redis({_KEY.format(run_id): _stamped(1)}):
            assert await _end_of_phase_upload_allowed(req, run_id) is False

    async def test_a_route_outside_the_set_is_refused(self):
        from terrapod.api.dependencies import _end_of_phase_upload_allowed

        run_id = str(uuid.uuid4())
        req = self._request("PUT", f"/api/terrapod/v1/runs/{run_id}/artifacts/state")
        with _redis({_KEY.format(run_id): _stamped(1)}):
            assert await _end_of_phase_upload_allowed(req, run_id) is False

    async def test_a_run_that_ended_long_ago_is_refused(self):
        from terrapod.api.dependencies import _end_of_phase_upload_allowed

        run_id = str(uuid.uuid4())
        req = self._request("PUT", f"/api/terrapod/v1/runs/{run_id}/artifacts/apply-log")
        with _redis({_KEY.format(run_id): _stamped(rts.TERMINAL_UPLOAD_GRACE_SECONDS + 5)}):
            assert await _end_of_phase_upload_allowed(req, run_id) is False

    async def test_the_grace_is_consulted_only_after_the_ordinary_check_refuses(self):
        """A live run must not pay a Redis read for a reprieve it does not need,
        and -- more to the point -- the ordinary path must stay exactly as it was.

        Asserted on the source rather than behaviourally: the property is the
        ORDER of two calls inside one boolean, which no fixture can observe.
        """
        import ast
        import inspect
        import textwrap

        from terrapod.api import dependencies

        src = textwrap.dedent(inspect.getsource(dependencies.get_current_user))
        tree = ast.parse(src)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.And):
                continue
            rendered = [ast.dump(v) for v in node.values]
            if any("is_run_token_usable" in r for r in rendered) and any(
                "_end_of_phase_upload_allowed" in r for r in rendered
            ):
                found.append(rendered)
        assert len(found) == 1, (
            "expected exactly one `and` joining the liveness check to the upload "
            f"grace; found {len(found)}"
        )
        usable_at = next(i for i, r in enumerate(found[0]) if "is_run_token_usable" in r)
        grace_at = next(i for i, r in enumerate(found[0]) if "_end_of_phase_upload_allowed" in r)
        assert usable_at < grace_at, (
            "the grace is being consulted before the ordinary liveness check, so "
            "every live run now pays for it and the ordinary path has changed"
        )
