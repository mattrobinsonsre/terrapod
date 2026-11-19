"""Posting the aggregate statuses: dedupe, lock, fail-safety, connection choice."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.services import vcs_aggregate_post as post
from terrapod.services.vcs_aggregate_status import Aggregate, PREvaluation, Verdict
from terrapod.services.vcs_pr_decisions import Stored, repo_scope

REPO = "org/repo"
SHA = "a" * 40
CONN = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="", status="active")
SCOPE = repo_scope(CONN, REPO)


class _KV:
    """The Redis commands the poster uses; a MULTI applies all-or-nothing."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def set(self, key, value, *, ex=None, nx=False):
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        self._check()
        return self.values.get(key)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            n += int(self.values.pop(k, None) is not None) + int(
                self.hashes.pop(k, None) is not None
            )
        return n

    async def exists(self, key):
        return int(key in self.values or key in self.hashes)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        self.ttls[key] = seconds

    async def eval(self, script, n, key, token, *args):
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            self.ttls[key] = int(args[0])
            return 1
        del self.values[key]
        return 1

    def pipeline(self, transaction=True):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def q(*a, **kw):
            self.ops.append((name, a, kw))
            return self

        return q

    async def execute(self, raise_on_error=True):
        self.r._check()
        return [await getattr(self.r, n)(*a, **kw) for n, a, kw in self.ops]


@pytest.fixture
def kv():
    r = _KV()
    with patch("terrapod.redis.client.get_redis_client", return_value=r):
        yield r


def _agg(plan="success", policy="success", apply=None, url=None):
    def v(state):
        return Verdict(state, f"{state} d", url)

    return Aggregate(v(plan), v(policy), v(apply) if apply else None)


@pytest.fixture
def provider():
    with (
        patch.object(post.github_service, "create_commit_status", new=AsyncMock()) as gh,
        patch.object(post.gitlab_service, "create_commit_status", new=AsyncMock()) as gl,
    ):
        yield SimpleNamespace(github=gh, gitlab=gl)


def _stub(agg=None, evaluation=None, sessions=()):
    agg = agg or _agg()
    return (
        patch.object(post, "_evaluate", new=AsyncMock(return_value=(agg, evaluation))),
        patch.object(post, "_sessions", new=AsyncMock(return_value=list(sessions))),
        patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
    )


async def _publish(*patches, **kw):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await post.publish(MagicMock(), CONN, REPO, 7, SHA, **kw)


def _green_markers(kv):
    return [
        k
        for k, v in kv.values.items()
        if k.startswith("tp:vcs_agg_last:") and not v.startswith("failed:")
    ]


def _contexts(mock):
    return [c.kwargs["context"] for c in mock.await_args_list]


class TestDedupe:
    async def test_an_identical_second_publish_makes_no_call(self, kv, provider):
        patches = _stub(_agg(apply="success"))
        await _publish(*patches)
        assert len(provider.github.await_args_list) == 3
        await _publish(*_stub(_agg(apply="success")))
        assert len(provider.github.await_args_list) == 3

    async def test_a_changed_verdict_posts_that_name_only(self, kv, provider):
        await _publish(*_stub(_agg("pending", "success")))
        provider.github.reset_mock()
        await _publish(*_stub(_agg("success", "success")))
        assert _contexts(provider.github) == ["terrapod/plan"]

    async def test_apply_is_posted_only_when_there_is_a_verdict(self, kv, provider):
        await _publish(*_stub(_agg()))
        assert "terrapod/apply" not in _contexts(provider.github)

    async def test_names_come_from_the_config(self, kv, provider):
        from terrapod.config import settings

        names = settings.vcs.commit_statuses
        old = names.plan_name
        names.plan_name = "ci/tf-plan"
        try:
            await _publish(*_stub())
        finally:
            names.plan_name = old
        assert "ci/tf-plan" in _contexts(provider.github)

    async def test_the_last_posted_marker_is_written_only_after_a_2xx(self, kv, provider):
        provider.github.side_effect = httpx.ConnectError("boom")
        await _publish(*_stub())
        assert not _green_markers(kv)
        provider.github.side_effect = None
        with patch.object(post, "_now", return_value=post._now() + 3600):
            await _publish(*_stub())
        assert len(_green_markers(kv)) == 2


class TestFailures:
    def _status_error(self, code):
        resp = MagicMock()
        resp.status_code = code
        return httpx.HTTPStatusError(str(code), request=MagicMock(), response=resp)

    async def test_a_4xx_is_final_logged_not_raised_and_not_retried_next_time(self, kv, provider):
        provider.github.side_effect = self._status_error(422)
        assert await _publish(*_stub()) is not None
        first = len(provider.github.await_args_list)
        await _publish(*_stub())
        assert len(provider.github.await_args_list) == first
        assert not _green_markers(kv)
        assert [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:")]

    async def test_a_changed_verdict_is_tried_again_after_a_4xx(self, kv, provider):
        provider.github.side_effect = self._status_error(422)
        await _publish(*_stub(_agg("pending")))
        provider.github.side_effect = None
        provider.github.reset_mock()
        await _publish(*_stub(_agg("success")))
        assert "terrapod/plan" in _contexts(provider.github)

    async def test_a_429_is_transient_not_final(self, kv, provider):
        provider.github.side_effect = self._status_error(429)
        await _publish(*_stub())
        assert not [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:")]

    async def test_a_5xx_is_not_raised_and_leaves_no_marker(self, kv, provider):
        provider.github.side_effect = self._status_error(502)
        await _publish(*_stub())
        assert not _green_markers(kv)

    async def test_redis_down_posts_nothing(self, kv, provider):
        kv.fail = True
        assert await _publish(*_stub()) is None
        provider.github.assert_not_awaited()

    async def test_no_redis_client_posts_nothing(self, provider):
        with patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("no")):
            assert await _publish(*_stub()) is None
        provider.github.assert_not_awaited()

    async def test_a_missing_record_computes_pending_never_green(self, kv, provider):
        # real aggregate over no rows and no record: pending, not success
        with (
            patch.object(post, "load", new=AsyncMock(return_value=None)),
            patch.object(post, "collect_head_rows", new=AsyncMock(return_value=[])),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        states = {c.kwargs["context"]: c.kwargs["state"] for c in provider.github.await_args_list}
        assert states == {"terrapod/plan": "pending", "terrapod/policy": "pending"}


class TestTheLock:
    async def test_a_held_lock_posts_nothing_and_marks_the_commit_dirty(self, kv, provider):
        kv.values[f"tp:vcs_agg_lock:{SCOPE}:{SHA}"] = "someone-else"
        assert await _publish(*_stub()) is None
        provider.github.assert_not_awaited()
        assert "*" in kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"]

    async def test_the_lock_is_released_and_the_mark_cleared(self, kv, provider):
        await _publish(*_stub())
        assert not [k for k in kv.values if k.startswith("tp:vcs_agg_lock")]
        assert not kv.hashes

    async def test_a_change_arriving_mid_publish_makes_the_holder_go_round_again(
        self, kv, provider
    ):
        calls = []

        async def evaluate(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                # another caller arrives while the first one works
                kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"] = {"*": "1"}
                return _agg("pending", "pending"), None
            return _agg("success", "success"), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        assert len(calls) == 2
        last = {c.kwargs["context"]: c.kwargs["state"] for c in provider.github.await_args_list}
        assert last["terrapod/plan"] == "success"

    async def test_rounds_are_bounded(self, kv, provider):
        calls = []

        async def evaluate(*a, **k):
            calls.append(1)
            kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"] = {"*": "1"}
            return _agg(), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        assert len(calls) == 3

    async def test_a_failure_while_holding_still_releases_the_lock(self, kv, provider):
        with (
            patch.object(post, "_evaluate", new=AsyncMock(side_effect=RuntimeError("db"))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
        ):
            assert await post.publish(MagicMock(), CONN, REPO, 7, SHA) is None
        assert not [k for k in kv.values if k.startswith("tp:vcs_agg_lock")]
        provider.github.assert_not_awaited()


class TestSessions:
    def _sess(self, state, comment=None):
        return SimpleNamespace(
            id=uuid.uuid4(), state=state, status_comment_id=comment, vcs_connection=CONN
        )

    async def test_a_closed_session_gets_nothing(self, kv, provider):
        assert await _publish(*_stub(sessions=[self._sess("closed")])) is None
        provider.github.assert_not_awaited()

    @pytest.mark.parametrize("state", ["open", "merged"])
    async def test_open_and_merged_sessions_are_posted(self, kv, provider, state):
        await _publish(*_stub(sessions=[self._sess(state)]))
        provider.github.assert_awaited()

    async def test_no_session_is_posted(self, kv, provider):
        await _publish(*_stub())
        provider.github.assert_awaited()

    async def test_the_comment_refresh_is_enqueued_only_when_asked_and_something_posted(
        self, kv, provider
    ):
        sess = self._sess("open")
        with patch.object(post, "enqueue_trigger", new=AsyncMock()) as enq:
            await _publish(*_stub(sessions=[sess]), enqueue_refresh=True)
            enq.assert_awaited_once()
            assert enq.await_args.kwargs["dedup_key"].startswith(f"vcs_status:{sess.id}:agg:{SHA}:")
            enq.reset_mock()
            await _publish(*_stub(sessions=[sess]), enqueue_refresh=True)
            enq.assert_not_awaited()  # nothing changed, nothing posted

    async def test_the_dispatcher_style_call_enqueues_no_refresh(self, kv, provider):
        with patch.object(post, "enqueue_trigger", new=AsyncMock()) as enq:
            await _publish(*_stub(sessions=[self._sess("open")]))
            enq.assert_not_awaited()


class TestProviders:
    async def test_github_gets_pending_for_running(self, kv, provider):
        await _publish(*_stub(_agg("running", "success")))
        states = {c.kwargs["context"]: c.kwargs["state"] for c in provider.github.await_args_list}
        assert states["terrapod/plan"] == "pending"

    async def test_gitlab_gets_failed_and_never_canceled(self, kv, provider):
        gl = SimpleNamespace(id=uuid.uuid4(), provider="gitlab", server_url="", status="active")
        scope = repo_scope(gl, REPO)
        with (
            patch.object(post, "_evaluate", new=AsyncMock(return_value=(_agg("failure"), None))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=gl)),
        ):
            await post.publish_scope(MagicMock(), gl, scope, REPO, 7, SHA)
        provider.github.assert_not_awaited()
        states = {c.kwargs["context"]: c.kwargs["state"] for c in provider.gitlab.await_args_list}
        assert states["terrapod/plan"] == "failed"
        assert "canceled" not in states.values()


class TestTargetUrl:
    def test_a_blocker_page_wins_while_not_green(self):
        v = Verdict("failure", "d", "https://tp/run")
        assert post._target_url(v, "https://gh/pull/1#issuecomment-2") == "https://tp/run"

    def test_green_prefers_the_comment(self):
        v = Verdict("success", "d", "https://tp/run")
        assert post._target_url(v, "https://gh/pull/1#issuecomment-2").endswith("issuecomment-2")

    def test_no_run_and_no_comment_is_empty(self):
        assert post._target_url(Verdict("success", "d", None), "") == ""

    def test_pending_with_no_run_falls_back_to_the_comment(self):
        assert post._target_url(Verdict("pending", "d", None), "https://x#c") == "https://x#c"

    def test_anchors(self):
        s = [SimpleNamespace(status_comment_id="42")]
        gh = SimpleNamespace(provider="github", server_url="")
        gl = SimpleNamespace(provider="gitlab", server_url="https://gl.example/")
        assert (
            post._comment_anchor(gh, REPO, 7, s)
            == "https://github.com/org/repo/pull/7#issuecomment-42"
        )
        assert (
            post._comment_anchor(gl, REPO, 7, s)
            == "https://gl.example/org/repo/-/merge_requests/7#note_42"
        )
        assert post._comment_anchor(gh, REPO, 7, [SimpleNamespace(status_comment_id=None)]) == ""


class TestWhichConnectionPosts:
    def _conn(self, status="active"):
        return SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="", status=status)

    def _repo(self, *conns):
        return patch.object(post, "_repository_connections", AsyncMock(return_value=list(conns)))

    async def test_the_first_active_connection_of_the_repository_posts(self):
        a, b = self._conn(), self._conn()
        with self._repo(a, b):
            assert await post._posting_connection(MagicMock(), SCOPE, CONN) is a

    async def test_an_inactive_one_is_skipped(self):
        a, b = self._conn("disabled"), self._conn()
        with self._repo(a, b):
            assert await post._posting_connection(MagicMock(), SCOPE, CONN) is b

    async def test_no_workspace_falls_back_to_the_callers_connection(self):
        with self._repo():
            assert await post._posting_connection(MagicMock(), SCOPE, CONN) is CONN
            assert await post._posting_connection(MagicMock(), SCOPE, None) is None

    async def test_nothing_active_posts_nothing(self):
        off = self._conn("disabled")
        with self._repo(off):
            assert await post._posting_connection(MagicMock(), SCOPE, CONN) is None


class TestWhenThePassRepublishes:
    def _ev(self, candidate=False):
        return PREvaluation(
            complete=True,
            affected=(),
            not_evaluated=(),
            undecided=(),
            fork_skipped=(),
            apply_then_merge_candidate=candidate,
        )

    async def test_a_changed_record_republishes(self, kv):
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(), True))

    async def test_a_steady_green_pr_is_left_alone(self, kv):
        for ctx in ("terrapod/plan", "terrapod/policy"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"
        kv.values[post._checked_key(SCOPE, SHA)] = str(post._now())
        assert not await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(), False))

    async def test_nothing_posted_yet_republishes(self, kv):
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(), False))

    async def test_a_non_green_last_post_republishes(self, kv):
        kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"] = "pending:abc"
        kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/policy"] = "success:abc"
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(), False))

    async def test_apply_counts_only_for_an_apply_then_merge_repository(self, kv):
        for ctx in ("terrapod/plan", "terrapod/policy"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(True), False))

    async def test_a_redis_error_republishes(self, kv):
        kv.fail = True
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(self._ev(), False))

    async def test_publish_pass_ignores_an_empty_or_failed_store(self, kv):
        from terrapod.services.vcs_pr_decisions import PRDecisions

        await post.publish_pass(PRDecisions(), {})
        await post.publish_pass(PRDecisions(), {(SCOPE, 7, SHA): Stored(None, False)})


class TestAFailedDowngradeNeverLeavesAGreenMarker:
    def _status_error(self, code, headers=None, text=""):
        resp = MagicMock()
        resp.status_code = code
        resp.headers = headers or {}
        resp.text = text
        return httpx.HTTPStatusError(str(code), request=MagicMock(), response=resp)

    def _last(self, kv, context="terrapod/plan"):
        return kv.values.get(f"tp:vcs_agg_last:{SCOPE}:{SHA}:{context}")

    async def test_a_failed_post_overwrites_a_success_marker(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        assert self._last(kv).startswith("success:")
        provider.github.side_effect = self._status_error(502)
        await _publish(*_stub(_agg("failure", "success")))
        assert self._last(kv).startswith("failed:")

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("boom"),
            httpx.HTTPStatusError(
                "422", request=MagicMock(), response=MagicMock(status_code=422, headers={}, text="")
            ),
            httpx.HTTPStatusError(
                "403", request=MagicMock(), response=MagicMock(status_code=403, headers={}, text="")
            ),
            httpx.HTTPStatusError(
                "429", request=MagicMock(), response=MagicMock(status_code=429, headers={}, text="")
            ),
        ],
    )
    async def test_no_kind_of_failure_leaves_the_success_marker(self, kv, provider, error):
        await _publish(*_stub(_agg("success", "success")))
        provider.github.side_effect = error
        await _publish(*_stub(_agg("failure", "success")))
        assert not self._last(kv).startswith("success:")

    async def test_the_pass_keeps_retrying_until_the_downgrade_lands(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        provider.github.side_effect = self._status_error(502)
        await _publish(*_stub(_agg("failure", "success")))
        clock = [post._now()]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            for _ in range(3):  # every due cycle: the record did not change, yet it republishes
                clock[0] += 3600
                assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
                await _publish(*_stub(_agg("failure", "success")))
            provider.github.side_effect = None
            provider.github.reset_mock()
            clock[0] += 3600
            await _publish(*_stub(_agg("failure", "success")))
        assert _contexts(provider.github) == ["terrapod/plan"]
        assert self._last(kv).startswith("failure:")


def _ev(candidate=False):
    return PREvaluation(
        complete=True,
        affected=(),
        not_evaluated=(),
        undecided=(),
        fork_skipped=(),
        apply_then_merge_candidate=candidate,
    )


class TestRateLimitsAndAuthAreNotFinal:
    def _err(self, code, headers=None, text=""):
        resp = MagicMock()
        resp.status_code = code
        resp.headers = headers or {}
        resp.text = text
        return httpx.HTTPStatusError(str(code), request=MagicMock(), response=resp)

    def _rejected(self, kv):
        return [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:")]

    @pytest.mark.parametrize(
        ("headers", "text"),
        [
            ({"Retry-After": "30"}, ""),
            ({"X-RateLimit-Remaining": "0"}, ""),
            ({}, "You have exceeded a secondary rate limit"),
        ],
    )
    async def test_a_rate_limited_403_is_transient(self, kv, provider, headers, text):
        provider.github.side_effect = self._err(403, headers, text)
        await _publish(*_stub())
        assert not self._rejected(kv)

    async def test_a_plain_403_is_remembered_only_briefly(self, kv, provider):
        provider.github.side_effect = self._err(403)
        await _publish(*_stub())
        key = [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:") and k.endswith("plan")][
            0
        ]
        assert kv.ttls[key] == post._REJECTED_AUTH_TTL < post._REJECTED_TTL

    async def test_a_401_is_remembered_only_briefly(self, kv, provider):
        provider.github.side_effect = self._err(401)
        await _publish(*_stub())
        key = [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:") and k.endswith("plan")][
            0
        ]
        assert kv.ttls[key] == post._REJECTED_AUTH_TTL

    async def test_a_422_is_remembered_for_the_long_time(self, kv, provider):
        provider.github.side_effect = self._err(422)
        await _publish(*_stub())
        key = [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:") and k.endswith("plan")][
            0
        ]
        assert kv.ttls[key] == post._REJECTED_TTL

    async def test_a_rejection_is_counted_and_logged_without_secrets(self, kv, provider):
        provider.github.side_effect = self._err(422)
        with (
            patch.object(post, "VCS_AGGREGATE_STATUS_REJECTED") as counter,
            patch.object(post, "logger") as log,
        ):
            await _publish(*_stub())
        counter.labels.assert_any_call(provider="github", status_code="422")
        kwargs = log.error.call_args_list[0].kwargs
        assert kwargs["repo"] == REPO and kwargs["context"] == "terrapod/plan"
        assert kwargs["status_code"] == 422
        assert "token" not in str(kwargs).lower()

    async def test_a_transient_failure_is_not_counted_as_a_rejection(self, kv, provider):
        provider.github.side_effect = self._err(502)
        with patch.object(post, "VCS_AGGREGATE_STATUS_REJECTED") as counter:
            await _publish(*_stub())
        counter.labels.assert_not_called()


class TestGitlabSameStateRepost:
    async def test_cannot_transition_is_a_noop_not_an_error(self, kv, provider):
        gl = SimpleNamespace(id=uuid.uuid4(), provider="gitlab", server_url="", status="active")
        scope = repo_scope(gl, REPO)
        resp = MagicMock()
        resp.status_code = 400
        resp.headers = {}
        resp.text = '{"message":"Cannot transition status via :run from :running"}'
        provider.gitlab.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=resp
        )
        with (
            patch.object(post, "_evaluate", new=AsyncMock(return_value=(_agg("running"), None))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=gl)),
            patch.object(post, "VCS_AGGREGATE_STATUS_REJECTED") as counter,
            patch.object(post, "logger") as log,
        ):
            await post.publish_scope(MagicMock(), gl, scope, REPO, 7, SHA)
        counter.labels.assert_not_called()
        log.error.assert_not_called()
        assert not [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:")]
        assert kv.values[f"tp:vcs_agg_last:{scope}:{SHA}:terrapod/plan"].startswith("running:")

    async def test_another_400_is_still_a_rejection(self, kv, provider):
        resp = MagicMock()
        resp.status_code = 400
        resp.headers = {}
        resp.text = "bad name"
        provider.github.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=resp
        )
        await _publish(*_stub())
        assert [k for k in kv.values if k.startswith("tp:vcs_agg_rejected:")]


class TestAChangeArrivingDuringAnotherPublishKeepsItsOverlay:
    """The dispatcher can act on a trigger before the run row's commit lands, so
    a loser of the lock must hand its overlay to the holder, not just a flag."""

    async def test_the_holder_applies_the_losers_overlay(self, kv, provider):
        run_id = uuid.uuid4()
        seen = []

        async def evaluate(db, scope, pr, sha, overlays):
            seen.append(dict(overlays or {}))
            if len(seen) == 1:
                # another replica's dispatcher arrives while this one works
                lost = await post.publish(
                    MagicMock(), CONN, REPO, 7, SHA, overlay=(run_id, "discarded", None)
                )
                assert lost is None
                return _agg("success", "success"), None
            return _agg("failure", "failure"), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        assert seen[0] == {}
        assert seen[1] == {run_id: [("discarded", None)]}
        states = {c.kwargs["context"]: c.kwargs["state"] for c in provider.github.await_args_list}
        assert states["terrapod/plan"] == "failure"

    async def test_every_waiting_overlay_is_applied(self, kv, provider):
        a, b = uuid.uuid4(), uuid.uuid4()
        seen = []

        async def evaluate(db, scope, pr, sha, overlays):
            seen.append(dict(overlays or {}))
            if len(seen) == 1:
                await post.publish(MagicMock(), CONN, REPO, 7, SHA, overlay=(a, "discarded", None))
                await post.publish(MagicMock(), CONN, REPO, 7, SHA, overlay=(b, "planned", True))
            return _agg(), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        assert seen[1] == {a: [("discarded", None)], b: [("planned", True)]}

    async def test_a_failure_after_taking_the_overlays_puts_them_back(self, kv, provider):
        run_id = uuid.uuid4()
        with (
            patch.object(post, "_evaluate", new=AsyncMock(side_effect=RuntimeError("db"))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA, overlay=(run_id, "errored", None))
        dirty = kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"]
        assert f"{run_id}|errored" in dirty

    def test_the_furthest_state_wins_and_a_terminal_one_beats_the_rest(self):
        run = SimpleNamespace(id=uuid.uuid4(), status="planning", has_changes=None)
        over = {run.id: [("planned", True), ("discarded", None), ("planning", None)]}
        assert post._overlaid(run, over) == ("discarded", None)
        assert post._overlaid(run, {run.id: [("planning", None)]}) == ("planning", None)

    def test_the_rows_own_newer_state_beats_an_older_overlay(self):
        run = SimpleNamespace(id=uuid.uuid4(), status="planned", has_changes=True)
        assert post._overlaid(run, {run.id: [("planning", None)]}) == ("planned", True)

    def test_a_terminal_row_is_never_overridden(self):
        run = SimpleNamespace(id=uuid.uuid4(), status="errored", has_changes=None)
        assert post._overlaid(run, {run.id: [("planned", True)]}) == ("errored", None)


class TestTheLockCanExpireMidPublish:
    async def test_a_publish_that_lost_its_lock_posts_nothing_more(self, kv, provider):
        lock = f"tp:vcs_agg_lock:{SCOPE}:{SHA}"

        async def first_post_then_expiry(*a, **k):
            kv.values.pop(lock, None)  # the lock's TTL runs out during the first post

        provider.github.side_effect = first_post_then_expiry
        await _publish(*_stub(_agg("success", "success", "success")))
        assert len(provider.github.await_args_list) == 1
        # whoever holds the lock next must recompute: the commit is marked dirty
        assert "*" in kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"]

    async def test_the_lock_is_extended_before_each_post(self, kv, provider):
        lock = f"tp:vcs_agg_lock:{SCOPE}:{SHA}"
        ttls = []

        async def record(*a, **k):
            ttls.append(kv.ttls[lock])

        provider.github.side_effect = record
        await _publish(*_stub(_agg("success", "success", "success")))
        assert ttls == [post._LOCK_TTL] * 3

    async def test_a_stolen_lock_is_not_released_by_the_old_holder(self, kv, provider):
        lock = f"tp:vcs_agg_lock:{SCOPE}:{SHA}"

        async def steal(*a, **k):
            kv.values[lock] = "someone-else"

        provider.github.side_effect = steal
        await _publish(*_stub(_agg("success", "success", "success")))
        assert kv.values[lock] == "someone-else"

    def test_the_ttl_covers_the_providers_worst_case_retry_budget(self):
        from terrapod.services import github_service, gitlab_service

        budget = max(
            (github_service._MAX_RETRIES + 1) * github_service._MAX_RETRY_WAIT_SECONDS,
            (gitlab_service._MAX_RETRIES + 1) * gitlab_service._MAX_RETRY_WAIT_SECONDS,
        )
        assert budget <= post._LOCK_TTL
        assert post._DIRTY_TTL > post._LOCK_TTL


class TestAChangeThatCannotBeServedIsNotSilentlyDropped:
    async def test_exhausted_rounds_warn_and_invalidate_the_last_posted_markers(self, kv, provider):
        for ctx in ("terrapod/plan", "terrapod/policy", "terrapod/apply"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"

        async def evaluate(*a, **k):
            kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"] = {"*": "1"}
            return _agg(), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
            patch.object(post, "logger") as log,
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        log.warning.assert_called()
        assert not [k for k in kv.values if k.startswith("tp:vcs_agg_last:")]
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_publish_that_converges_leaves_the_markers_alone(self, kv, provider):
        await _publish(*_stub())
        assert [k for k in kv.values if k.startswith("tp:vcs_agg_last:")]


class TestThePassPostsThroughTheRepositoryConnection:
    async def test_it_is_the_one_the_repository_picks_not_one_from_the_record(self, kv):
        from terrapod.services.vcs_pr_decisions import PRDecisions

        picked = SimpleNamespace(id=uuid.uuid4(), provider="github", server_url="", status="active")

        class _Ctx:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *a):
                return False

        dec = PRDecisions()
        dec.polled(SimpleNamespace(id=uuid.uuid4(), name="a", vcs_workflow=""), CONN, REPO)
        scope = dec.scopes()[0]
        with (
            patch.object(post, "get_db_session", return_value=_Ctx()),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=picked)),
            patch.object(post, "publish_scope", new=AsyncMock()) as pub,
        ):
            await post.publish_pass(dec, {(scope, 7, SHA): Stored(_ev(), True)})
        pub.assert_awaited_once()
        assert pub.await_args.args[1] is picked


class TestSwitchedOff:
    async def _off(self):
        from terrapod.config import settings

        settings.vcs.commit_statuses.enabled = False
        return settings

    async def test_publish_computes_and_posts_nothing(self, kv, provider):
        settings = await self._off()
        try:
            with patch.object(post, "_evaluate", new=AsyncMock()) as evaluate:
                out = await post.publish(MagicMock(), CONN, REPO, 7, SHA)
        finally:
            settings.vcs.commit_statuses.enabled = True
        assert out is None
        evaluate.assert_not_awaited()
        provider.github.assert_not_awaited()
        assert not kv.values and not kv.hashes

    async def test_the_pass_does_nothing(self, kv):
        settings = await self._off()
        try:
            with patch.object(post, "get_db_session") as session:
                await post.publish_pass(post.PRDecisions(), {(SCOPE, 7, SHA): Stored(_ev(), True)})
        finally:
            settings.vcs.commit_statuses.enabled = True
        session.assert_not_called()


class TestTheLockHelperKeepsItsOriginalSignature:
    def test_no_unused_ttl_parameter(self):
        import inspect

        from terrapod.services import vcs_status_comment

        assert list(inspect.signature(vcs_status_comment._acquire_comment_lock).parameters) == [
            "redis",
            "key",
        ]


class TestComputeRecorded:
    """What the comment uses: the same evaluation as `publish`, or nothing."""

    async def test_no_record_gives_none_not_an_invented_pending(self, kv):
        with (
            patch.object(post, "load", new=AsyncMock(return_value=None)),
            patch.object(post, "collect_head_rows", new=AsyncMock(return_value=[])),
        ):
            assert await post.compute_recorded(MagicMock(), CONN, REPO, 7, SHA) is None
            # `compute` (what publish posts) still reports pending for the same state
            assert (await post.compute(MagicMock(), CONN, REPO, 7, SHA)).plan.state == "pending"

    async def test_a_record_gives_exactly_what_compute_gives(self, kv):
        with (
            patch.object(post, "load", new=AsyncMock(return_value=_ev())),
            patch.object(post, "collect_head_rows", new=AsyncMock(return_value=[])),
        ):
            got = await post.compute_recorded(MagicMock(), CONN, REPO, 7, SHA)
            assert got == await post.compute(MagicMock(), CONN, REPO, 7, SHA)
            assert got.plan.state == "success"


class TestAnAbortedPublishNeverLeavesAStaleGreen:
    """The lock holder gives up before any post: the waiting change must not be forgotten."""

    async def _green(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        assert all(
            v.startswith("success:")
            for k, v in kv.values.items()
            if k.startswith("tp:vcs_agg_last:")
        )

    def _no_green_marker(self, kv):
        return not [
            k
            for k, v in kv.items()
            if k.startswith("tp:vcs_agg_last:") and v.startswith("success:")
        ]

    async def test_a_database_error_before_any_post(self, kv, provider):
        await self._green(kv, provider)
        run_id = uuid.uuid4()
        with (
            patch.object(post, "_evaluate", new=AsyncMock(side_effect=RuntimeError("db glitch"))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
        ):
            await post.publish(MagicMock(), CONN, REPO, 7, SHA, overlay=(run_id, "errored", None))
        assert self._no_green_marker(kv.values)
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_cancelled_holder_still_restores_and_propagates(self, kv, provider):
        await self._green(kv, provider)
        with (
            patch.object(post, "_evaluate", new=AsyncMock(side_effect=asyncio.CancelledError())),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            pytest.raises(asyncio.CancelledError),
        ):
            await post.publish(
                MagicMock(), CONN, REPO, 7, SHA, overlay=(uuid.uuid4(), "errored", None)
            )
        assert self._no_green_marker(kv.values)
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
        assert f"tp:vcs_agg_lock:{SCOPE}:{SHA}" not in kv.values

    async def test_a_redis_error_while_extending_the_lock(self, kv, provider):
        await self._green(kv, provider)
        real_eval = kv.eval

        async def flaky(script, n, key, token, *args):
            if "expire" in script:
                raise ConnectionError("redis blip")
            return await real_eval(script, n, key, token, *args)

        kv.eval = flaky
        await _publish(*_stub(_agg("failure", "failure")), overlay=(uuid.uuid4(), "errored", None))
        assert self._no_green_marker(kv.values)
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_the_lock_lost_before_the_first_post(self, kv, provider):
        await self._green(kv, provider)
        lock = f"tp:vcs_agg_lock:{SCOPE}:{SHA}"

        async def evaluate(*a, **k):
            kv.values.pop(lock, None)  # expired while computing
            return _agg("failure", "failure"), None

        with (
            patch.object(post, "_evaluate", new=evaluate),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            provider.github.reset_mock()
            await post.publish(
                MagicMock(), CONN, REPO, 7, SHA, overlay=(uuid.uuid4(), "errored", None)
            )
        provider.github.assert_not_awaited()
        assert self._no_green_marker(kv.values)
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_waiting_change_alone_makes_the_pass_republish(self, kv):
        for ctx in ("terrapod/plan", "terrapod/policy"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"
        kv.values[post._checked_key(SCOPE, SHA)] = str(post._now())
        assert not await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
        kv.hashes[f"tp:vcs_agg_dirty:{SCOPE}:{SHA}"] = {"*": "1"}
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))


class TestBackingOff:
    def _err(self, code, headers=None, text=""):
        resp = MagicMock()
        resp.status_code = code
        resp.headers = headers or {}
        resp.text = text
        return httpx.HTTPStatusError(str(code), request=MagicMock(), response=resp)

    def _marker(self, kv, ctx="terrapod/plan"):
        return kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"]

    def _parse(self, marker):
        failed = post._parse_failed(marker)
        assert failed is not None
        return failed.count, failed.until

    async def test_the_marker_backs_off_exponentially_to_a_cap(self, kv, provider):
        provider.github.side_effect = self._err(502)
        clock = [1_000_000.0]
        gaps = []
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            for _ in range(8):
                await _publish(*_stub(_agg("success", "success")))
                _, until = self._parse(self._marker(kv))
                gaps.append(until - clock[0])
                clock[0] = until + 1
        assert gaps[0] == post._BACKOFF_BASE
        assert gaps[1] == 2 * post._BACKOFF_BASE
        assert gaps == sorted(gaps)
        assert max(gaps) == post._BACKOFF_CAP

    async def test_nothing_is_posted_again_before_the_marker_is_due(self, kv, provider):
        provider.github.side_effect = self._err(502)
        clock = [1_000_000.0]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            await _publish(*_stub(_agg("success", "success")))
            first = len(provider.github.await_args_list)
            clock[0] += 5
            await _publish(*_stub(_agg("success", "success")))
            assert len(provider.github.await_args_list) == first
            assert not await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
            clock[0] += post._BACKOFF_BASE
            assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_changed_verdict_is_not_held_back_by_the_old_backoff(self, kv, provider):
        provider.github.side_effect = self._err(502)
        await _publish(*_stub(_agg("success", "success")))
        provider.github.side_effect = None
        provider.github.reset_mock()
        await _publish(*_stub(_agg("failure", "success")))
        assert "terrapod/plan" in _contexts(provider.github)

    async def test_a_rate_limit_stops_every_post_on_the_connection(self, kv, provider):
        provider.github.side_effect = self._err(403, {"Retry-After": "120"})
        await _publish(*_stub(_agg("success", "success", "success")))
        assert len(provider.github.await_args_list) == 1  # the other two contexts did not try
        backoff = kv.values[post._backoff_key(CONN.id)]
        assert kv.ttls[post._backoff_key(CONN.id)] == 120
        assert float(backoff) > post._now()

    async def test_another_pr_head_on_the_same_connection_waits_too(self, kv, provider):
        provider.github.side_effect = self._err(429, {"Retry-After": "90"})
        await _publish(*_stub())
        provider.github.reset_mock()
        provider.github.side_effect = None
        other = "b" * 40
        with (
            patch.object(post, "_evaluate", new=AsyncMock(return_value=(_agg(), None))),
            patch.object(post, "_sessions", new=AsyncMock(return_value=[])),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
        ):
            await post.publish(MagicMock(), CONN, REPO, 8, other)
        provider.github.assert_not_awaited()

    async def test_the_backoff_ends(self, kv, provider):
        provider.github.side_effect = self._err(429, {"Retry-After": "60"})
        clock = [1_000_000.0]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            await _publish(*_stub())
            provider.github.side_effect = None
            provider.github.reset_mock()
            clock[0] += 61
            kv.values.pop(post._backoff_key(CONN.id), None)  # the key's TTL
            await _publish(*_stub())
        provider.github.assert_awaited()

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Retry-After": "45"}, 45),
            ({"Retry-After": "99999"}, 900),
            ({}, 60),
        ],
    )
    def test_the_delay_honours_retry_after_and_is_capped(self, headers, expected):
        resp = MagicMock()
        resp.headers = headers
        assert post._rate_limit_delay(resp, now=1_000_000.0) == expected

    def test_the_delay_honours_the_reset_time(self):
        resp = MagicMock()
        resp.headers = {"X-RateLimit-Reset": "1000300"}
        assert post._rate_limit_delay(resp, now=1_000_000.0) == 300

    async def test_a_pass_skips_a_connection_that_is_backed_off(self, kv):
        from terrapod.services.vcs_pr_decisions import PRDecisions

        kv.values[post._backoff_key(CONN.id)] = str(post._now() + 100)
        ev = PREvaluation(True, (), (), (), (), False)
        db = MagicMock()

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False

        dec = PRDecisions()
        dec.polled(SimpleNamespace(id=uuid.uuid4(), name="a", vcs_workflow=""), CONN, REPO)
        scope = dec.scopes()[0]
        with (
            patch.object(post, "get_db_session", return_value=_Ctx()),
            patch.object(post, "_posting_connection", new=AsyncMock(return_value=CONN)),
            patch.object(post, "publish_scope", new=AsyncMock()) as pub,
        ):
            await post.publish_pass(dec, {(scope, 7, SHA): Stored(ev, True)})
        pub.assert_not_awaited()


class TestPostOutcomesAreCounted:
    def _counts(self, counter):
        return {call.kwargs["outcome"] for call in counter.labels.call_args_list}

    async def test_ok_transient_and_rejected(self, kv, provider):
        resp = MagicMock()
        resp.headers = {}
        resp.text = ""
        with patch.object(post, "VCS_AGGREGATE_STATUS_POSTS") as counter:
            await _publish(*_stub())
            assert self._counts(counter) == {"ok"}
            counter.reset_mock()
            resp.status_code = 502
            provider.github.side_effect = httpx.HTTPStatusError(
                "x", request=MagicMock(), response=resp
            )
            await _publish(*_stub(_agg("failure", "failure")))
            assert self._counts(counter) == {"transient"}
            counter.reset_mock()
            resp.status_code = 422
            await _publish(*_stub(_agg("pending", "pending")))
            assert self._counts(counter) == {"rejected"}
            counter.labels.assert_any_call(provider="github", outcome="rejected")


class TestThePostsAreAttributed:
    async def test_every_provider_call_runs_under_a_named_source(self, kv):
        from terrapod.services import vcs_rate_limit

        seen = []

        async def spy(*a, **k):
            seen.append(vcs_rate_limit.current_source())

        with patch.object(post.github_service, "create_commit_status", new=spy):
            await _publish(*_stub())
        assert seen and set(seen) == {"aggregate-statuses"}
        assert vcs_rate_limit.current_source() == vcs_rate_limit.SOURCE_UNKNOWN  # restored

    async def test_the_pass_is_attributed_too(self, kv):
        import inspect

        assert "vcs_source(" in inspect.getsource(post.publish_pass)


class TestAGitlabSameStateRepostIsDocumentedInTheCode:
    def test_the_noop_explains_itself(self):
        import inspect

        src = inspect.getsource(post._post_if_changed)
        assert "never update" in src


class TestAFailedDowngradeIsRetriedWithinOnePollInterval:
    """The provider shows green; the failure that should replace it must not wait out an
    exponential backoff built for upgrades."""

    def _err(self, code):
        resp = MagicMock()
        resp.status_code = code
        resp.headers = {}
        resp.text = ""
        return httpx.HTTPStatusError(str(code), request=MagicMock(), response=resp)

    def _interval(self):
        from terrapod.config import settings

        return settings.vcs.poll_interval_seconds

    async def test_five_outages_then_recovery_posts_within_one_interval(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))  # the provider shows green
        clock = [post._now()]
        interval = self._interval()
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            provider.github.side_effect = self._err(502)
            clock[0] += interval
            await _publish(*_stub(_agg("failure", "failure")))  # the run event that fails
            for _ in range(5):
                clock[0] += interval
                assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
                before = len(provider.github.await_args_list)
                await _publish(*_stub(_agg("failure", "failure")))
                assert len(provider.github.await_args_list) > before  # tried again this cycle
            provider.github.side_effect = None
            provider.github.reset_mock()
            clock[0] += interval
            assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
            await _publish(*_stub(_agg("failure", "failure")))
        assert "terrapod/plan" in _contexts(provider.github)
        assert kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"].startswith("failure:")

    async def test_the_green_flag_is_carried_across_failures(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        provider.github.side_effect = self._err(502)
        clock = [post._now()]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            for _ in range(3):
                clock[0] += self._interval()
                await _publish(*_stub(_agg("failure", "failure")))
        marker = post._parse_failed(kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"])
        assert marker.green and marker.count >= 3
        assert marker.until - clock[0] <= self._interval()

    async def test_a_rate_limit_pause_still_wins(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "300"}
        resp.text = ""
        provider.github.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=resp
        )
        clock = [post._now()]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            clock[0] += self._interval()
            await _publish(*_stub(_agg("failure", "failure")))
            marker = post._parse_failed(kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"])
            assert marker.until - clock[0] >= 300
            provider.github.reset_mock()
            provider.github.side_effect = None
            clock[0] += self._interval()
            await _publish(*_stub(_agg("failure", "failure")))
        provider.github.assert_not_awaited()

    async def test_a_failed_upgrade_keeps_the_exponential_backoff(self, kv, provider):
        provider.github.side_effect = self._err(502)
        clock = [post._now()]
        gaps = []
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            await _publish(*_stub(_agg("pending", "pending")))  # red/pending shown (never green)
            provider.github.reset_mock()
            for _ in range(5):
                clock[0] += 1000
                await _publish(*_stub(_agg("success", "success")))
                marker = post._parse_failed(
                    kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"]
                )
                gaps.append(marker.until - clock[0])
                assert not marker.green
        assert gaps == sorted(gaps) and gaps[-1] > self._interval()

    async def test_a_refused_downgrade_waits_out_the_refusal_memory(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        provider.github.side_effect = self._err(422)
        await _publish(*_stub(_agg("failure", "failure")))
        marker = post._parse_failed(kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"])
        assert marker.until - post._now() > post._REJECTED_TTL - 5


class TestARefusedHeadIsNotReEvaluatedEveryCycle:
    async def test_the_pass_skips_it_before_taking_the_lock_while_the_memory_is_live(
        self, kv, provider
    ):
        resp = MagicMock()
        resp.status_code = 422
        resp.headers = {}
        resp.text = ""
        provider.github.side_effect = httpx.HTTPStatusError(
            "422", request=MagicMock(), response=resp
        )
        clock = [post._now()]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            await _publish(*_stub(_agg("pending", "pending")))
            clock[0] += 120
            assert not await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
            clock[0] += 3600
            assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_changed_record_still_republishes(self, kv, provider):
        resp = MagicMock()
        resp.status_code = 422
        resp.headers = {}
        resp.text = ""
        provider.github.side_effect = httpx.HTTPStatusError(
            "422", request=MagicMock(), response=resp
        )
        await _publish(*_stub(_agg("pending", "pending")))
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), True))


class TestASteadyGreenIsRecheckedNowAndThen:
    async def test_a_green_that_has_not_been_looked_at_for_a_while_is_recomputed(self, kv):
        for ctx in ("terrapod/plan", "terrapod/policy"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"
        kv.values[post._checked_key(SCOPE, SHA)] = str(post._now() - post._GREEN_RECHECK - 1)
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_green_never_checked_is_recomputed(self, kv):
        for ctx in ("terrapod/plan", "terrapod/policy"):
            kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:{ctx}"] = "success:abc"
        assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_publish_stamps_the_check_so_the_pass_leaves_it_alone(self, kv, provider):
        await _publish(*_stub())
        assert kv.values[post._checked_key(SCOPE, SHA)]
        assert not await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))

    async def test_a_recheck_posts_only_on_change(self, kv, provider):
        await _publish(*_stub(_agg("success", "success")))
        provider.github.reset_mock()
        kv.values[post._checked_key(SCOPE, SHA)] = str(post._now() - post._GREEN_RECHECK - 1)
        await _publish(*_stub(_agg("success", "success")))
        provider.github.assert_not_awaited()  # unchanged: nothing posted
        await _publish(*_stub(_agg("failure", "success")))
        assert _contexts(provider.github) == ["terrapod/plan"]  # a stale green is caught

    async def test_a_lost_dispatcher_publish_is_bounded(self, kv, provider):
        # the dispatcher's publish never reached the dirty hash; only the periodic recheck can see it
        await _publish(*_stub(_agg("success", "success")))
        clock = [post._now() + post._GREEN_RECHECK + 1]
        with patch.object(post, "_now", side_effect=lambda: clock[0]):
            assert await post._needs_publish(kv, SCOPE, SHA, Stored(_ev(), False))
            provider.github.reset_mock()
            await _publish(*_stub(_agg("failure", "failure")))
        assert "terrapod/plan" in _contexts(provider.github)


class TestTheGatesBlockSaysWhenAStatusDidNotLand:
    async def test_notes_come_from_the_last_posted_markers(self, kv):
        names = SimpleNamespace(
            plan_name="terrapod/plan", policy_name="terrapod/policy", apply_name="terrapod/apply"
        )
        kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/plan"] = (
            "failed:2:99999999999.0::failure:abc"
        )
        kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/policy"] = "success:abc"
        kv.values[f"tp:vcs_agg_rejected:{SCOPE}:{SHA}:terrapod/apply"] = "failure:abc"
        kv.values[f"tp:vcs_agg_last:{SCOPE}:{SHA}:terrapod/apply"] = (
            "failed:1:99999999999.0::failure:abc"
        )
        notes = await post.posting_notes(SCOPE, SHA, names)
        assert "retrying" in notes["terrapod/plan"]
        assert "terrapod/policy" not in notes
        assert "refused" in notes["terrapod/apply"]

    async def test_a_redis_error_gives_no_notes(self, kv):
        kv.fail = True
        names = SimpleNamespace(plan_name="a", policy_name="b", apply_name="c")
        assert await post.posting_notes(SCOPE, SHA, names) == {}
