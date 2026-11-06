"""Tests for runner listener — SSE + polling fallback."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module

RunnerListener = listener_module.RunnerListener


@pytest.fixture(autouse=True)
def fresh_shutdown_event():
    """Replace the module-level shutdown event with one bound to the test loop."""
    new_event = asyncio.Event()
    old_event = listener_module._shutdown
    listener_module._shutdown = new_event
    yield new_event
    listener_module._shutdown = old_event


def _make_listener(shutdown_event: asyncio.Event, **overrides) -> RunnerListener:
    """Create a listener with mocked identity and config."""
    with patch("terrapod.runner.listener.load_runner_config") as mock_cfg:
        # __init__ now reads listener knobs off RunnerConfig (#617) — give the
        # mock real int defaults so comparisons (active >= max_concurrent) work.
        #
        # The namespaces need real STRINGS for the same class of reason, since
        # kubernetes-client 37: its models are pydantic now, so V1ObjectMeta
        # VALIDATES its fields and a MagicMock namespace raises
        # `ValidationError: Input should be a valid string` from inside
        # `_create_vars_secret`. On 36.x the models were plain classes and a
        # mock sailed through. Values match the RunnerConfig defaults.
        mock_cfg.return_value = MagicMock(
            definitions=[],
            runner_namespace="terrapod-runners",
            listener_namespace="terrapod",
            heartbeat_interval=60,
            max_concurrent=3,
            health_port=8081,
            sse_retry_interval=5,
            poll_interval=30,
            sse_read_timeout=30,
            sse_max_age=600,
        )
        listener = RunnerListener()

    listener.identity = MagicMock()
    listener.identity.listener_id = "test-listener-id"
    listener.identity.api_url = "http://api:8000"
    listener.identity.certificate_pem = "CERT"
    listener._identity_ready = True

    for k, v in overrides.items():
        setattr(listener, k, v)

    return listener


# ── Poll loop ────────────────────────────────────────────────────────


class TestPollLoop:
    async def test_poll_loop_calls_handle_run_available(self, fresh_shutdown_event):
        """Poll loop calls _handle_run_available on each tick."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05)
        call_count = 0

        async def mock_handle():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        assert call_count >= 3

    async def test_poll_loop_respects_shutdown(self, fresh_shutdown_event):
        """Poll loop exits when shutdown is signaled."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=60)

        # Signal shutdown after a short delay
        async def signal_shutdown():
            await asyncio.sleep(0.05)
            fresh_shutdown_event.set()

        asyncio.create_task(signal_shutdown())
        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        # If we get here, the loop exited properly

    async def test_poll_loop_handles_errors_gracefully(self, fresh_shutdown_event):
        """Poll loop logs errors but continues polling."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05)
        call_count = 0

        async def mock_handle():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("API unreachable")
            fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        assert call_count >= 3  # Continued despite errors


# ── Handle run available ─────────────────────────────────────────────


class TestHandleRunAvailable:
    async def test_skips_when_at_max_concurrent(self, fresh_shutdown_event):
        """Does not claim if already at max concurrent launches."""
        listener = _make_listener(fresh_shutdown_event, _max_concurrent=2, _active_launches=2)

        with patch("httpx.AsyncClient") as mock_client_cls:
            await listener._handle_run_available()
            mock_client_cls.assert_not_called()

    async def test_handles_no_runs_available(self, fresh_shutdown_event):
        """204 response means no runs — graceful no-op."""
        listener = _make_listener(fresh_shutdown_event)

        empty = MagicMock()
        empty.status_code = 204

        mock_client = AsyncMock()
        mock_client.get.side_effect = [empty]
        listener._http_client = mock_client

        listener._launch_run = AsyncMock()
        with patch(
            # Patch the function the drain actually calls. Leaving this on the
            # old name is a FALSE GREEN on a workstation: the real one loads
            # the developer's kubeconfig and answers `set()`, so the claim
            # proceeds, while a CI runner with no cluster fails closed and the
            # claim never happens (#2399).
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=set()),
        ):
            await listener._handle_run_available()

        mock_client.get.assert_called_once()

    async def test_claims_and_launches_run(self, fresh_shutdown_event):
        """Successful claim triggers _launch_run, then the drain loop stops on
        the next 204 (no more queued runs)."""
        listener = _make_listener(fresh_shutdown_event)

        run_data = {
            "data": {
                "id": "run-abc12345-abcd-1234-abcd-123456789012",
                "attributes": {"phase": "plan"},
            }
        }
        claimed = MagicMock()
        claimed.status_code = 200
        claimed.json.return_value = run_data
        claimed.raise_for_status = MagicMock()
        empty = MagicMock()
        empty.status_code = 204

        mock_client = AsyncMock()
        # One run, then the queue is empty — the loop drains and stops (#750).
        mock_client.get.side_effect = [claimed, empty]
        listener._http_client = mock_client

        listener._launch_run = AsyncMock()
        with patch(
            # Patch the function the drain actually calls. Leaving this on the
            # old name is a FALSE GREEN on a workstation: the real one loads
            # the developer's kubeconfig and answers `set()`, so the claim
            # proceeds, while a CI runner with no cluster fails closed and the
            # claim never happens (#2399).
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=set()),
        ):
            await listener._handle_run_available()

        listener._launch_run.assert_called_once()
        assert mock_client.get.call_count == 2  # claim, then 204
        assert listener._active_launches == 0  # Decremented after launch


# ── SSE dispatch ─────────────────────────────────────────────────────


class TestDispatchEvent:
    async def test_dispatches_run_available(self, fresh_shutdown_event):
        """run_available event triggers _handle_run_available."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_run_available = AsyncMock()

        await listener._dispatch_event("run_available", json.dumps({"pool_id": "test"}))

        listener._handle_run_available.assert_called_once()

    async def test_dispatches_check_job_status(self, fresh_shutdown_event):
        """check_job_status event triggers _handle_check_job_status."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_check_job_status = AsyncMock()

        data = {"run_id": "r1", "job_name": "j1", "job_namespace": "ns"}
        await listener._dispatch_event("check_job_status", json.dumps(data))

        listener._handle_check_job_status.assert_called_once_with(data)

    async def test_dispatches_cancel_job(self, fresh_shutdown_event):
        """cancel_job event triggers _handle_cancel_job."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_cancel_job = AsyncMock()

        data = {"job_name": "j1", "job_namespace": "ns"}
        await listener._dispatch_event("cancel_job", json.dumps(data))

        listener._handle_cancel_job.assert_called_once_with(data)

    async def test_ignores_invalid_json(self, fresh_shutdown_event):
        """Invalid JSON is logged and skipped."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_run_available = AsyncMock()

        await listener._dispatch_event("run_available", "not-json")

        listener._handle_run_available.assert_not_called()

    async def test_ignores_unknown_event(self, fresh_shutdown_event):
        """Unknown event type is ignored."""
        listener = _make_listener(fresh_shutdown_event)
        # Should not raise
        await listener._dispatch_event("unknown_event", json.dumps({"x": 1}))


# ── Concurrent SSE + poll safety ─────────────────────────────────────


class TestConcurrentSafety:
    async def test_sse_and_poll_can_both_trigger_claims(self, fresh_shutdown_event):
        """Both SSE and poll can trigger _handle_run_available concurrently."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05, _max_concurrent=5)
        claims = []

        async def mock_handle():
            claims.append(1)
            if len(claims) >= 5:
                fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        # Run poll loop alongside a simulated SSE trigger
        async def sse_trigger():
            for _ in range(3):
                await asyncio.sleep(0.02)
                if not fresh_shutdown_event.is_set():
                    await listener._handle_run_available()

        await asyncio.wait_for(
            asyncio.gather(listener._poll_loop(), sse_trigger()),
            timeout=2,
        )

        assert len(claims) >= 5  # Both paths contributed


# ── Launch failure reporting ─────────────────────────────────────────


class TestLaunchFailureReporting:
    """Pre-Job failures must be reported to the API so the run errors out fast.

    Without this, /runner-token 401 / create_job exception / etc. would leave
    the run in `planning` until the reconciler's launch_timeout (5 min). Active
    reporting from the listener gives operators an immediate, accurate error
    message at the API rather than a generic timeout 5 min later.
    """

    async def test_runner_token_failure_reports_errored(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        listener._get_runner_token = AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
        listener._http_client = AsyncMock()

        await listener._launch_run("abc-123", {"phase": "plan"})

        listener._http_client.patch.assert_awaited_once()
        kwargs = listener._http_client.patch.await_args.kwargs
        body = kwargs["json"]
        assert body["status"] == "errored"
        assert "runner token" in body["error_message"].lower()
        assert "401" in body["error_message"]

    async def test_create_job_failure_reports_errored(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        listener._get_runner_token = AsyncMock(return_value="runtok:xyz")
        listener._http_client = AsyncMock()
        listener.runner_config = MagicMock()

        with (
            patch("terrapod.runner.job_template.build_job_spec", return_value={"kind": "Job"}),
            patch(
                "terrapod.runner.job_manager.create_job",
                AsyncMock(side_effect=RuntimeError("kubernetes API down")),
            ),
        ):
            await listener._launch_run("abc-123", {"phase": "plan"})

        listener._http_client.patch.assert_awaited_once()
        body = listener._http_client.patch.await_args.kwargs["json"]
        assert body["status"] == "errored"
        assert (
            "k8s job" in body["error_message"].lower() or "create" in body["error_message"].lower()
        )

    async def test_report_failure_swallows_patch_errors(self, fresh_shutdown_event):
        """Best-effort: a failed PATCH must not crash the listener event loop."""
        listener = _make_listener(fresh_shutdown_event)
        listener._http_client = AsyncMock()
        listener._http_client.patch = AsyncMock(side_effect=RuntimeError("network down"))

        # Must not raise
        await listener._report_launch_failed("abc-123", "boom")


# ── Auth Secret naming (phase collision regression) ───────────────────


class TestAuthSecretNaming:
    """Plan and apply phases of the same run must use distinct Secret names.

    Confirm-and-Apply transitions a run from `planned` to `applying` within
    seconds. The plan Job's auth Secret has an ownerReference back to the
    plan Job, but K8s GC doesn't always finalise the cascade before the
    apply phase tries to create its own Secret. Pre-fix, both phases used
    `tprun-{run_short}-auth` → second create → 409 AlreadyExists → entire
    apply Job fails to launch.

    Per-phase names (`tprun-{run_short}-{phase}-auth`) eliminate the race
    by giving each Job its own Secret keyed on the same phase suffix the
    Job name already uses.
    """

    async def test_plan_and_apply_use_distinct_secret_names(self, fresh_shutdown_event):
        """build_job_spec sees a phase-aware secret name; plan != apply."""
        listener = _make_listener(fresh_shutdown_event)
        listener._get_runner_token = AsyncMock(return_value="runtok:xyz")
        listener._http_client = AsyncMock()
        listener._create_auth_secret = AsyncMock(return_value="ok")
        listener.runner_config = MagicMock()

        captured = {}

        def fake_build(**kwargs):
            captured.setdefault("calls", []).append(kwargs)
            return {"kind": "Job"}

        with (
            patch("terrapod.runner.job_template.build_job_spec", side_effect=fake_build),
            patch("terrapod.runner.job_manager.create_job", AsyncMock(return_value="job-x")),
            patch("terrapod.runner.job_manager.get_job_uid", AsyncMock(return_value="uid-x")),
        ):
            run_id = "0123456789abcdef-zzzz"
            await listener._launch_run(run_id, {"phase": "plan"})
            await listener._launch_run(run_id, {"phase": "apply"})

        names = [c["auth_secret_name"] for c in captured["calls"]]
        assert names[0] != names[1], f"plan/apply must differ, got {names}"
        assert "plan" in names[0] and "apply" in names[1]
        # Same run prefix on both — only the phase suffix differs.
        prefix_plan = names[0].rsplit("-plan-", 1)[0]
        prefix_apply = names[1].rsplit("-apply-", 1)[0]
        assert prefix_plan == prefix_apply

    async def test_a_configure_gets_its_own_secret_names(self, fresh_shutdown_event):
        """The same race, with a third phase (#1972).

        A composite run is an apply Job and then a CONFIGURE Job driven by one
        Run, so the two exist seconds apart exactly as plan and apply do. Each
        Job's Secrets are owner-referenced to that Job, so a shared name does
        not merely collide on create -- the surviving Secret is GC'd when the
        earlier Job's cascade finalises, under a configure that is still using
        it.

        Asserted against BOTH earlier phases rather than just apply: a suffix
        that happened to equal the plan phase's would be just as broken, and
        a test comparing one pair cannot see it.
        """
        listener = _make_listener(fresh_shutdown_event)
        listener._get_runner_token = AsyncMock(return_value="runtok:xyz")
        listener._http_client = AsyncMock()
        listener._create_auth_secret = AsyncMock(return_value="ok")
        # Mocked because the env var below makes the launch path actually create
        # it, and the shared mock's namespace is a MagicMock that the k8s client
        # (pydantic since 37) refuses. Left real, the launch fails after the
        # names are captured -- so the test would still pass, beside a broken
        # path, which is the kind of green that teaches nothing.
        listener._create_vars_secret = AsyncMock(return_value="ok")
        listener.runner_config = MagicMock()

        captured: dict[str, list] = {}

        def fake_build(**kwargs):
            captured.setdefault("calls", []).append(kwargs)
            return {"kind": "Job"}

        with (
            patch("terrapod.runner.job_template.build_job_spec", side_effect=fake_build),
            patch("terrapod.runner.job_manager.create_job", AsyncMock(return_value="job-x")),
            patch("terrapod.runner.job_manager.get_job_uid", AsyncMock(return_value="uid-x")),
        ):
            run_id = "0123456789abcdef-zzzz"
            for phase in ("plan", "apply", "configure"):
                # One env var, because `vars_secret_name` is composed only when
                # the run HAS variables -- without it the name is legitimately
                # "" for every phase and the assertion below would be comparing
                # three empty strings and passing for the wrong reason.
                await listener._launch_run(
                    run_id,
                    {"phase": phase, "env-vars": [{"key": "FOO", "value": "bar"}]},
                )

        for key in ("auth_secret_name", "vars_secret_name"):
            names = [c[key] for c in captured["calls"]]
            assert len(set(names)) == 3, f"{key} must differ per phase, got {names}"
            assert "configure" in names[2], names[2]
            # Same run prefix throughout -- only the phase suffix moves.
            prefixes = {
                n.replace(f"-{p}-", "-", 1)
                for n, p in zip(names, ("plan", "apply", "configure"), strict=True)
            }
            assert len(prefixes) == 1, prefixes


# ── Per-run vars Secret ──────────────────────────────────────────────


class TestCreateVarsSecret:
    """The vars Secret holds all variable values (terraform tfvars blob + env
    vars), with an ownerReference to the Job (cascade-GC), so values are never
    plaintext in the Job spec."""

    async def test_builds_secret_with_tfvars_blob_and_env_keys(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[
                    {"key": "cidr", "value": "10.0.0.0/16", "hcl": False},
                    {"key": "ports", "value": "[80, 443]", "hcl": True},
                ],
                env_vars=[{"key": "AWS_SECRET_ACCESS_KEY", "value": "shh"}],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
            )
        core_api.create_namespaced_secret.assert_called_once()
        body = core_api.create_namespaced_secret.call_args.kwargs["body"]
        sd = body.string_data
        # Env var value is a Secret key.
        assert sd["AWS_SECRET_ACCESS_KEY"] == "shh"
        # Terraform vars are a JSON blob preserving hcl.
        import json as _json

        blob = _json.loads(sd["terraform.tfvars.json"])
        # Both names, always equal: the vars Secret feeds a runner that may be
        # older than this API and reads `hcl` (#1435).
        ports = next(v for v in blob if v["key"] == "ports")
        assert ports["value"] == "[80, 443]"
        assert ports["structured"] is True and ports["hcl"] is True
        # ownerReference back to the Job (cascade GC).
        owner = body.metadata.owner_references[0]
        assert owner.kind == "Job" and owner.uid == "uid-1"

    async def test_ansible_credentials_become_their_own_secret_key(self, fresh_shutdown_event):
        """#1933. Its own key rather than sharing `git-auth.json`, because the
        Job mounts the ansible blob only for a configure -- folded in, these
        would ride into every plan's Job, and a plan executes a pull request's
        code.
        """
        import json as _json

        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-configure-vars",
                "r1",
                terraform_vars=[],
                env_vars=[],
                job_name="tprun-r1-configure",
                job_uid="uid-1",
                ansible_auth=[
                    {
                        "category": "ansible_vault_auth",
                        "key": "prod",
                        "value": '{"password":"p"}',
                    }
                ],
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        assert "ansible-auth.json" in sd
        assert "git-auth.json" not in sd, "the two channels must stay separate"
        entry = _json.loads(sd["ansible-auth.json"])[0]
        assert entry == {
            "category": "ansible_vault_auth",
            "key": "prod",
            "value": '{"password":"p"}',
        }

    async def test_no_ansible_auth_key_when_none_was_delivered(self, fresh_shutdown_event):
        """The ordinary state of every plan and apply."""
        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[],
                env_vars=[{"key": "FOO", "value": "bar"}],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        assert "ansible-auth.json" not in sd

    async def test_no_tfvars_key_when_only_env_vars(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[],
                env_vars=[{"key": "FOO", "value": "bar"}],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        assert "terraform.tfvars.json" not in sd
        assert sd["FOO"] == "bar"


# ── Launch namespace propagation (regression) ────────────────────────


class TestLaunchNamespace:
    """Regression: the launch path must create the Job AND look up its UID (for
    the auth-Secret ownerReference) in the *configured* runner namespace.

    get_job_uid() previously omitted the namespace and fell back to a stale
    default decoupled from the configured runner namespace, so the auth-Secret
    step did a `get jobs` in the wrong namespace than the Job was created in —
    a 403 on every launch when the two differed (e.g. single-namespace eval)."""

    async def test_launch_uses_configured_runner_namespace(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        listener.runner_config.runner_namespace = "custom-runner-ns"

        listener._get_runner_token = AsyncMock(return_value="runtok:abc")
        listener._create_auth_secret = AsyncMock()
        listener._read_ca_bundle_pem = MagicMock(return_value="")
        listener._report_launch_failed = AsyncMock()
        listener._auth_headers = MagicMock(return_value={})

        spec = {"metadata": {"name": "tprun-r1-plan", "namespace": "custom-runner-ns"}}
        with (
            patch("terrapod.runner.job_template.build_job_spec", return_value=spec),
            patch(
                "terrapod.runner.job_manager.create_job",
                new=AsyncMock(return_value="tprun-r1-plan"),
            ) as mock_create,
            patch(
                "terrapod.runner.job_manager.get_job_uid",
                new=AsyncMock(return_value="uid-1"),
            ) as mock_uid,
            patch("terrapod.runner.listener.arequest_with_retry", new=AsyncMock()),
        ):
            await listener._launch_run("r1", {"phase": "plan"})

        listener._report_launch_failed.assert_not_called()
        mock_create.assert_awaited_once()
        assert mock_create.await_args.kwargs["namespace"] == "custom-runner-ns"
        mock_uid.assert_awaited_once()
        assert mock_uid.await_args.kwargs["namespace"] == "custom-runner-ns"


class TestReJoinAttemptsAreRateLimited:
    """The runaway, bounded without losing prompt recovery (#2352).

    `_auth_failures` deliberately survives a failed re-join -- see
    `test_a_failed_re_join_reports_false_and_does_not_clear_the_count`, whose
    reason still holds: `_rejoin_unlocked` clears the credentials Secret before
    it re-establishes, so a listener whose re-join failed has no identity at
    all and making it wait for another three rejections prolongs an outage it
    cannot otherwise leave.

    What was unbounded was the consequence. Every later rejection -- one every
    30-90s -- re-entered the path, each time deleting the Secret and spending
    another join-token use, so on a `max_uses: 1` pool the first failed re-join
    made the listener permanently unjoinable. A floor on the interval keeps the
    prompt first retry and bounds the rest.
    """

    async def test_the_counter_still_survives_a_failed_rejoin(self, fresh_shutdown_event):
        """The pre-existing property, asserted here too so this change cannot
        quietly take it away."""
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock(side_effect=RuntimeError("no"))

        for _ in range(listener_module._REJOIN_THRESHOLD):
            rejoined = await listener._note_auth_failure(source="sse")

        assert rejoined is False
        assert listener._auth_failures == listener_module._REJOIN_THRESHOLD

    async def test_a_second_rejoin_is_refused_inside_the_interval(self, fresh_shutdown_event):
        """The fix. Before it, every rejection after a failed re-join deleted
        the Secret and spent another join-token use."""
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock(side_effect=RuntimeError("no"))

        for _ in range(listener_module._REJOIN_THRESHOLD):
            await listener._note_auth_failure(source="sse")
        assert listener._rejoin_unlocked.await_count == 1

        for _ in range(5):
            await listener._note_auth_failure(source="sse")
        assert listener._rejoin_unlocked.await_count == 1, (
            "five further rejections spent five more join-token uses"
        )

    async def test_it_is_attempted_again_once_the_interval_has_passed(self, fresh_shutdown_event):
        """Bounded, not disabled -- otherwise the fix would be an outage of its
        own, since a listener that cannot re-join cannot recover at all."""
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock(side_effect=RuntimeError("no"))

        for _ in range(listener_module._REJOIN_THRESHOLD):
            await listener._note_auth_failure(source="sse")
        assert listener._rejoin_unlocked.await_count == 1

        # Wind the clock past the floor rather than sleeping through it.
        listener._last_rejoin_attempt -= listener_module._REJOIN_MIN_INTERVAL_SECONDS + 1
        await listener._note_auth_failure(source="sse")
        assert listener._rejoin_unlocked.await_count == 2

    async def test_a_successful_rejoin_still_clears_the_count(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock()

        for _ in range(listener_module._REJOIN_THRESHOLD):
            rejoined = await listener._note_auth_failure(source="sse")

        assert rejoined is True
        assert listener._auth_failures == 0
        assert listener._rejoin_generation == 1

    def test_the_interval_and_threshold_are_the_values_we_chose(self):
        """Literals, not re-derivations. Winding the clock by the constant
        proves the gate consults it and nothing about whether it is sane: 0
        would disable the bound entirely, and an hour would delay every real
        recovery by an hour."""
        assert listener_module._REJOIN_MIN_INTERVAL_SECONDS == 60.0
        assert listener_module._REJOIN_THRESHOLD == 3

    async def test_a_refused_rejoin_reports_FALSE_so_the_caller_backs_off(
        self, fresh_shutdown_event
    ):
        """The cooldown's return value, which is what the loops act on.

        `test_a_second_rejoin_is_refused_inside_the_interval` above asserts only
        that `_rejoin_unlocked` is not called again — and that passed while the
        skip returned `None`, indistinguishable from success. `_sse_loop` acts on
        the RETURN VALUE: both its `continue`s jump past the
        `wait_for(_shutdown.wait(), timeout=sse_retry_interval)` backoff, so a
        skip reported as success made the listener reconnect at full network
        speed — one HTTPS request and one warning per iteration — for the rest
        of the interval. Before the cooldown those `continue`s were
        self-throttling, because each was preceded by a real re-join doing
        network and Secret work.
        """
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock()

        assert await listener._rejoin() is True, "a real re-join must report True"
        assert await listener._rejoin() is False, (
            "the cooldown skip reported success, so the SSE loop would retry "
            "immediately instead of backing off"
        )
        assert listener._rejoin_unlocked.await_count == 1

    async def test_note_auth_failure_passes_the_refusal_through(self, fresh_shutdown_event):
        """The 401 site reads this, not `_rejoin`, so the signal has to survive
        the hop."""
        listener = _make_listener(fresh_shutdown_event)
        listener._rejoin_unlocked = AsyncMock()

        for _ in range(listener_module._REJOIN_THRESHOLD):
            first = await listener._note_auth_failure(source="sse")
        assert first is True

        # Still past the threshold, still inside the interval.
        listener._auth_failures = listener_module._REJOIN_THRESHOLD
        assert await listener._note_auth_failure(source="sse") is False, (
            "a cooldown refusal was reported as a successful re-join"
        )

    async def test_another_loop_having_rejoined_still_reports_true(self, fresh_shutdown_event):
        """The one case where immediate retry IS right: somebody else got a
        fresh identity, so the caller should use it rather than back off.

        Driven as a real race -- `_rejoin_unlocked` awaits, so the two calls
        genuinely interleave and the loser observes the generation move while it
        waited on the lock. A sequential version cannot reach that branch at
        all: it reads the generation after the bump, so it matches and a second
        real re-join happens (which is how I first wrote this, and it failed).
        """
        listener = _make_listener(fresh_shutdown_event)
        ran = {"n": 0}

        async def _slow_rejoin():
            ran["n"] += 1
            await asyncio.sleep(0)

        listener._rejoin_unlocked = AsyncMock(side_effect=_slow_rejoin)
        first, second = await asyncio.gather(listener._rejoin(), listener._rejoin())

        assert ran["n"] == 1, "both callers performed a re-join"
        assert first is True and second is True, (
            "the caller that lost the race must still be told an identity is "
            "available, or it backs off from a fresh one"
        )
        assert listener._rejoin_generation == 1
