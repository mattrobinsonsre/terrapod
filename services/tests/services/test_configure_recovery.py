"""The remainder a partial configure leaves behind, and re-running only it (#1973).

The property under test throughout is a RELATIONSHIP between two states -- what
the run targeted, and what ansible reported -- so almost every fixture here
instantiates both. A fixture holding only rows cannot see the case the feature
exists for: under `serial:` + `any_errors_fatal` a host the play never reached
appears in no callback event and in no PLAY RECAP, so it is indistinguishable
from a host that does not exist unless you subtract from the targeted set.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import RunHostResult
from terrapod.services import configure_recovery as cr


class _SessionCM:
    """A real async context manager.

    `patch(...)` hands back a MagicMock, and setting `__aenter__` on a mock
    INSTANCE does nothing: async dunders are looked up on the type. So the fake
    session has to be a real class or the `async with` in the cycle fails for a
    reason that has nothing to do with the code under test.
    """

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _empty_db():
    """An AsyncMock session whose `execute` returns a SYNC result.

    On an `AsyncMock` every child attribute is also an AsyncMock, so
    `result.scalars()` would hand back a coroutine and `.all()` would fail on
    it -- an error about mock plumbing that reads like a bug in the sweep.
    """
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _row(host, **counters):
    """A real `RunHostResult`, not a stand-in.

    The model rather than a namespace so a renamed or dropped counter fails
    here, which is the whole reason `host_status` reads the columns it does.
    """
    defaults = {
        "ok": 0,
        "changed": 0,
        "failures": 0,
        "unreachable": 0,
        "skipped": 0,
        "rescued": 0,
        "ignored": 0,
        "saw_unreachable": False,
    }
    defaults.update(counters)
    return RunHostResult(id=uuid.uuid4(), host=host, **defaults)


class TestHostStatus:
    """The ONE producer. The router derived this inline until #1973 needed the
    same judgement to decide what a retry covers."""

    @pytest.mark.parametrize(
        "counters,expected",
        [
            ({"ok": 3}, "ok"),
            ({"ok": 2, "changed": 1}, "changed"),
            ({"failures": 1}, "failed"),
            ({"saw_unreachable": True}, "unreachable"),
            ({"ignored": 1}, "ignored-failure"),
            # A rescue leaves `failures` at zero, which is why `rescued` needs no
            # decision of its own: ansible counts it as success and so does this.
            ({"ok": 1, "changed": 1, "rescued": 1}, "changed"),
        ],
    )
    def test_the_derivation(self, counters, expected):
        assert cr.host_status(_row("h", **counters)) == expected

    def test_unreachable_wins_over_a_zero_counter(self):
        """`ignore_unreachable` leaves `unreachable=0` while the host was
        genuinely unreachable -- measured on #1928, where the callback fired
        against a summary reading `unreachable=0 ignored=1`. So the flag
        decides, not the counter, or an unreachable host reads as an ignored
        failure and drops out of the remainder."""
        row = _row("h", unreachable=0, ignored=1, saw_unreachable=True)
        assert cr.host_status(row) == "unreachable"


class TestTheRemainder:
    def test_a_host_the_play_never_reached_is_outstanding(self):
        """The case the subtraction exists for. `web3` is in the inventory and
        in no row at all, which is exactly what a `serial:` +
        `any_errors_fatal` abort produces."""
        out = cr.outstanding_from(
            {"web1", "web2", "web3"}, [_row("web1", ok=1), _row("web2", ok=1)]
        )
        assert out == ["web3"]

    def test_failed_and_unreachable_are_outstanding_and_successes_are_not(self):
        rows = [
            _row("ok1", ok=1),
            _row("changed1", changed=1),
            _row("failed1", failures=1),
            _row("unreach1", saw_unreachable=True),
        ]
        assert cr.outstanding_from({r.host for r in rows}, rows) == ["failed1", "unreach1"]

    def test_an_ignored_failure_is_NOT_outstanding(self):
        """**This contradicts the issue's own written formula**, which states the
        remainder as `snapshot - (ok u changed u skipped)` and so includes an
        ignored failure. Settled the other way (user, 2026-10-26): `ignore_errors:
        true` is the playbook author declaring that failure acceptable, so
        retrying re-runs a play they said was fine -- and under the automatic
        retry it would re-run to the attempt cap on every single run, for ever.
        """
        rows = [_row("tolerated", ok=1, ignored=1)]
        assert cr.outstanding_from({"tolerated"}, rows) == []

    def test_a_reported_failure_survives_an_unreadable_artifact(self):
        """An empty targeted set must not hide a failure ansible DID report.

        The union is what gives this: subtraction alone would return nothing
        here, so a storage outage would turn a genuinely failed host into
        "nothing outstanding" -- a silent, wrong, reassuring answer.
        """
        assert cr.outstanding_from(set(), [_row("failed1", failures=1)]) == ["failed1"]

    def test_the_order_is_stable(self):
        """The same remainder must produce the same `--limit` on every read, or
        two readers disagree about what a retry would do."""
        rows = [_row("b", failures=1), _row("a", failures=1)]
        assert cr.outstanding_from({"a", "b", "c"}, rows) == ["a", "b", "c"]
        assert cr.outstanding_from({"c", "b", "a"}, list(reversed(rows))) == ["a", "b", "c"]


class TestTheLimit:
    def test_hosts_join_on_a_colon(self):
        assert cr.remainder_limit(["web1", "web3"]) == "web1:web3"

    def test_an_empty_remainder_yields_an_empty_limit(self):
        """And every caller must refuse to queue on it. An empty
        `ansible_limit` means the workspace's own default, i.e. the WHOLE
        inventory -- the one outcome this feature exists to prevent."""
        assert cr.remainder_limit([]) == ""


class TestTheDenominatorFailsSafe:
    @pytest.mark.parametrize(
        "storage_behaviour",
        ["raises", "empty", "not-json", "not-an-object", "no-hosts-key"],
    )
    @pytest.mark.asyncio
    async def test_an_unusable_artifact_yields_no_targeted_hosts(self, storage_behaviour):
        """Which means an EMPTY remainder, deliberately.

        Reporting "every host needs configuring" because the denominator could
        not be read is the one answer that would have a retry touch the whole
        estate, so each of these degrades to nothing rather than to everything.
        """
        storage = MagicMock()
        if storage_behaviour == "raises":
            storage.get = AsyncMock(side_effect=RuntimeError("storage down"))
        else:
            payload = {
                "empty": b"",
                "not-json": b"<<<not json>>>",
                "not-an-object": b"[1, 2, 3]",
                "no-hosts-key": b'{"groups": {}}',
            }[storage_behaviour]
            storage.get = AsyncMock(return_value=payload)

        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4())
        with patch("terrapod.storage.get_storage", return_value=storage):
            assert await cr.targeted_hosts(run) == set()

    @pytest.mark.asyncio
    async def test_a_real_artifact_yields_its_hosts(self):
        """The positive control. Without it the parametrized test above passes
        whether the reader works or returns `set()` unconditionally."""
        storage = MagicMock()
        storage.get = AsyncMock(return_value=b'{"hosts": {"web1": {}, "web2": {}}}')
        # `ansible_limit` explicitly: a bare MagicMock attribute is TRUTHY, so
        # leaving it unset makes this run look limited and takes the #2690
        # fail-safe branch, which answers `set()`. Not noise -- it is what keeps
        # this test about the artifact reader.
        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), ansible_limit="")
        with patch("terrapod.storage.get_storage", return_value=storage):
            assert await cr.targeted_hosts(run) == {"web1", "web2"}


class TestTheAutomaticRetryIsOffUnlessAskedFor:
    @pytest.mark.asyncio
    async def test_a_zero_cap_does_nothing_at_all(self):
        """The default, and it diverges from `lifecycle_destroy_retries` (2) on
        purpose: a destroy is declarative and incremental so re-running it is
        safe by construction, while a playbook is whatever the operator wrote.

        Asserting it never opens a session, not merely that it queues nothing --
        a handler that reaches the database and then declines is still a query
        per cycle on every deployment that has not asked for this.
        """
        cfg = MagicMock(configure_retries=0, configure_retry_backoff_seconds=45)
        with (
            patch("terrapod.config.load_runner_config", return_value=cfg),
            patch("terrapod.db.session.get_db_session") as session,
        ):
            await cr.configure_retry_cycle()
        session.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_positive_cap_does_reach_the_database(self):
        """The positive control for the test above."""
        cfg = MagicMock(configure_retries=2, configure_retry_backoff_seconds=0)
        db = _empty_db()
        with (
            patch("terrapod.config.load_runner_config", return_value=cfg),
            patch("terrapod.db.session.get_db_session", return_value=_SessionCM(db)) as session,
        ):
            await cr.configure_retry_cycle()
        session.assert_called_once()
        db.execute.assert_awaited()


class TestTheAutomaticRetryGuards:
    def _errored_configure(self):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "errored"
        run.is_configure = True
        run.playbook = "site.yml"
        run.configuration_version_id = uuid.uuid4()
        run.updated_at = datetime.now(UTC) - timedelta(hours=1)
        return run

    @pytest.mark.asyncio
    async def test_a_superseded_run_is_left_alone(self):
        """A newer run -- a retry already queued, a manual retry, an apply --
        means we must not act. Without this guard two cycles, or two replicas
        racing, stack retries onto the same episode."""
        run = self._errored_configure()
        newer = MagicMock(id=uuid.uuid4())
        db = AsyncMock()
        with (
            patch.object(cr, "_latest_run", AsyncMock(return_value=newer)),
            patch.object(cr, "outstanding_hosts", AsyncMock(return_value=["web1"])) as hosts,
        ):
            await cr._retry_one(db, run, retries=2)
        hosts.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_exhausted_episode_is_left_for_a_human(self):
        run = self._errored_configure()
        db = AsyncMock()
        with (
            patch.object(cr, "_latest_run", AsyncMock(return_value=run)),
            patch.object(cr, "_consecutive_recovery_attempts", AsyncMock(return_value=2)),
            patch.object(cr, "outstanding_hosts", AsyncMock(return_value=["web1"])) as hosts,
        ):
            await cr._retry_one(db, run, retries=2)
        hosts.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_outstanding_queues_nothing(self):
        """The configure errored for a reason that is not a host set -- a
        malformed playbook, an unreadable inventory. Queueing here would re-run
        the whole limit, which is `remainder_limit`'s empty-case hazard arriving
        with no human in the loop."""
        run = self._errored_configure()
        db = AsyncMock()
        with (
            patch.object(cr, "_latest_run", AsyncMock(return_value=run)),
            patch.object(cr, "_consecutive_recovery_attempts", AsyncMock(return_value=0)),
            patch.object(cr, "outstanding_hosts", AsyncMock(return_value=[])),
            patch("terrapod.services.run_service.create_run", AsyncMock()) as create,
        ):
            await cr._retry_one(db, run, retries=2)
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_retry_carries_the_remainder_and_its_own_source(self):
        run = self._errored_configure()
        db = AsyncMock()
        ws = MagicMock(id=run.workspace_id)
        db.get = AsyncMock(return_value=ws)
        new = MagicMock(id=uuid.uuid4())
        with (
            patch.object(cr, "_latest_run", AsyncMock(return_value=run)),
            patch.object(cr, "_consecutive_recovery_attempts", AsyncMock(return_value=0)),
            patch.object(cr, "outstanding_hosts", AsyncMock(return_value=["web1", "web3"])),
            patch(
                "terrapod.services.run_service.create_run", AsyncMock(return_value=new)
            ) as create,
            patch("terrapod.services.run_service.queue_run", AsyncMock(return_value=new)),
        ):
            await cr._retry_one(db, run, retries=2)

        kwargs = create.await_args.kwargs
        assert kwargs["ansible_limit"] == "web1:web3"
        assert kwargs["is_configure"] is True
        assert kwargs["playbook"] == "site.yml"
        # Its own source, so `_consecutive_recovery_attempts` can count the
        # episode from the runs themselves. A MANUAL remainder retry keeps the
        # source it retries and so does not count against this cap -- the cap
        # bounds what the platform does unattended.
        assert kwargs["source"] == cr.RECOVERY_SOURCE

    @pytest.mark.asyncio
    async def test_the_sweep_selects_errored_configures_only(self):
        """Never `canceled`: that is a partial configure a person stopped, and
        resuming it automatically would override them. A human may still retry
        it to the remainder by hand."""
        cfg = MagicMock(configure_retries=2, configure_retry_backoff_seconds=0)
        db = _empty_db()
        with (
            patch("terrapod.config.load_runner_config", return_value=cfg),
            patch("terrapod.db.session.get_db_session", return_value=_SessionCM(db)),
        ):
            await cr.configure_retry_cycle()

        # The WHERE clause, not `str(stmt)`: rendering a `select(Run)` emits
        # every column of the table, so a name-in-the-statement assertion is
        # satisfied by the select list and passes with the predicate deleted.
        clause = str(db.execute.await_args.args[0].whereclause)
        assert "runs.is_configure" in clause
        assert "runs.status" in clause
        assert "canceled" not in clause


class TestThereIsOneProducerOfTheDerivation:
    """The router derived host status inline until #1973, and its own docstring
    gave the reason it must not: "derived here rather than stored, so one reader
    cannot disagree with another about what the counters mean". Once a retry
    needs the same judgement, a second copy is a second answer to "did this host
    configure" -- and the two would drift silently, because each would keep
    passing its own tests.

    Both halves are asserted, because a presence check is not an application
    check: that the rule exists in one place, AND that the router reaches it.
    """

    def _module_source(self, dotted):
        import importlib
        import pathlib

        return pathlib.Path(importlib.import_module(dotted).__file__).read_text()

    def test_only_one_module_spells_the_distinctive_status(self):
        """`ignored-failure` is the tell. `ok`, `changed` and `failed` are words
        a dozen modules use for other things, but nothing derives this status
        without naming it, so a reintroduced chain brings the literal with it.

        What this cannot catch, and the next person should know: a derivation
        that avoids the literal entirely. The import-and-call assertion below is
        what covers the realistic shape of that.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "terrapod"
        holders = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if '"ignored-failure"' in p.read_text()
        )
        assert holders == ["services/configure_recovery.py"], (
            f"the host-status derivation is spelled in {holders}; it must live in "
            "configure_recovery alone, or the per-host view and the retry's "
            "remainder can disagree about what a counter means"
        )

    def test_the_router_calls_it_rather_than_deriving_its_own(self):
        import ast

        tree = ast.parse(self._module_source("terrapod.api.routers.runs"))
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_host_result_json"
        )
        calls = {
            node.func.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "host_status" in calls, (
            "_host_result_json must call configure_recovery.host_status; it used to "
            "derive the status with its own if/elif chain"
        )
        # And no chain of its own alongside the call -- the shape that would let
        # both exist, with the call's result quietly overwritten.
        assert "ignored-failure" not in ast.unparse(fn)


class TestARefusedRetryIsSkippedRatherThanRaised:
    """`create_run` refuses when the workspace no longer declares a configure.

    #2116's rule is that every `create_run` caller decides what the refusal
    means FOR IT, and `test_plan_apply_enforcement.TestEveryCallSiteDecides`
    derives the call sites and fails on one that does not — which is how this
    was caught, as the sixteenth site.

    The answer here is catch-and-skip, because nobody asked for this run. The
    reason it must not merely propagate is specific: the attempt cap counts
    retries that were CREATED, so a refusal creates nothing, leaves the count
    at zero, and this path is re-reached on every cycle — for ever, about a
    workspace that is behaving exactly as declared.
    """

    def _errored_configure(self):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "errored"
        run.is_configure = True
        run.playbook = "site.yml"
        run.configuration_version_id = uuid.uuid4()
        run.updated_at = datetime.now(UTC) - timedelta(hours=1)
        return run

    @pytest.mark.parametrize("which", ["plan-apply", "configure"])
    @pytest.mark.asyncio
    async def test_both_refusals_skip(self, which):
        from terrapod.services import run_service, workspace_settings

        exc = (
            run_service.PlanApplyNotEnabled("no plan/apply")
            if which == "plan-apply"
            else workspace_settings.ConfigureNotEnabled("no configure")
        )
        run = self._errored_configure()
        db = AsyncMock()
        db.get = AsyncMock(return_value=MagicMock(id=run.workspace_id))
        with (
            patch.object(cr, "_latest_run", AsyncMock(return_value=run)),
            patch.object(cr, "_consecutive_recovery_attempts", AsyncMock(return_value=0)),
            patch.object(cr, "outstanding_hosts", AsyncMock(return_value=["web1"])),
            patch("terrapod.services.run_service.create_run", AsyncMock(side_effect=exc)),
            patch("terrapod.services.run_service.queue_run", AsyncMock()) as queue,
        ):
            # No exception escapes, and nothing is queued.
            await cr._retry_one(db, run, retries=2)
        queue.assert_not_called()


_ABSENT = object()


class TestTheRunsOwnLimitNarrowsTheDenominator:
    """#2690. The artifact is whole; the run's `--limit` is recorded beside it.

    Without the narrowing every consumer of "the hosts this run targeted" was
    handed the entire inventory: the log viewer offered a host filter covering
    hosts that cannot appear in the log, the per-host view reported every
    excluded host as `not-reached` -- a status that ASSERTS the run targeted it
    -- and an outstanding-only retry turned those into its own `--limit`, so
    retrying "just the outstanding hosts" of a three-host run reached the hosts
    the operator had excluded.
    """

    @staticmethod
    def _payload(*, targeted=_ABSENT, partial=False, hosts=("web1", "web2", "db1")) -> bytes:
        doc: dict = {"hosts": {h: {} for h in hosts}}
        if partial:
            doc["partial"] = True
        if targeted is not _ABSENT:
            doc["targeted"] = targeted
        return json.dumps(doc).encode()

    @staticmethod
    async def _targeted(payload: bytes, *, limit=None, reported=()) -> set[str]:
        storage = MagicMock()
        storage.get = AsyncMock(return_value=payload)
        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), ansible_limit=limit)
        with (
            patch("terrapod.storage.get_storage", return_value=storage),
            patch(
                "terrapod.services.host_results_service.reported_inventory_hosts",
                AsyncMock(return_value=set(reported)),
            ),
        ):
            return await cr.targeted_hosts(run)

    @pytest.mark.asyncio
    async def test_a_recorded_limit_narrows_to_what_it_selected(self) -> None:
        got = await self._targeted(
            self._payload(targeted={"limit": "web1", "hosts": ["web1"]}), limit="web1"
        )
        assert got == {"web1"}

    @pytest.mark.asyncio
    async def test_no_limit_means_the_whole_artifact(self) -> None:
        """Unchanged, and the common case. Such a run targets the artifact as
        stored, which `resolve_for_run` has already bounded by the workspace's
        `hard_limit`."""
        assert await self._targeted(self._payload()) == {"web1", "web2", "db1"}

    @pytest.mark.asyncio
    async def test_a_limit_that_selected_NOTHING_is_not_the_whole_estate(self) -> None:
        """An empty selection is a real answer and must not read as "no limit"."""
        got = await self._targeted(
            self._payload(targeted={"limit": "ghost", "hosts": []}), limit="ghost"
        )
        assert got == set()

    @pytest.mark.asyncio
    async def test_it_can_only_ever_NARROW(self) -> None:
        """A record naming a host the artifact does not contain cannot add it.
        Intersection rather than replacement is what makes that structural."""
        got = await self._targeted(
            self._payload(targeted={"limit": "web1:ghost", "hosts": ["web1", "ghost"]}),
            limit="web1:ghost",
        )
        assert got == {"web1"}

    @pytest.mark.asyncio
    async def test_the_partial_resolution_union_still_happens_AND_is_narrowed(self) -> None:
        """#2033's union must survive this: a workspace resolving dynamic
        sources on the runner has a short artifact, and the report is the longer
        side. So union first, then narrow -- `db2` is reported, not in the
        artifact, and inside the limit."""
        got = await self._targeted(
            self._payload(partial=True, targeted={"limit": "web1:db2", "hosts": ["web1", "db2"]}),
            limit="web1:db2",
            reported=("db2",),
        )
        assert got == {"web1", "db2"}

    @pytest.mark.parametrize(
        "bad", [{"limit": "x"}, {"hosts": "web1"}, {"hosts": None}, "web1", 7, None]
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_record_on_a_LIMITED_run_fails_safe_to_empty(self, bad) -> None:
        """The run carries a limit and the artifact does not say what it
        selected, so the denominator is unknowable. Empty, for the same reason
        every other degradation in this module is empty: "every host needs
        configuring" is the one answer that would have a retry touch the whole
        estate, and here it would reach precisely the excluded hosts."""
        assert await self._targeted(self._payload(targeted=bad), limit="web1") == set()

    @pytest.mark.asyncio
    async def test_an_unreadable_record_on_an_UNLIMITED_run_changes_nothing(self) -> None:
        """The pair to the above: with no limit there is nothing to narrow, so a
        junk key must not empty a perfectly good denominator."""
        got = await self._targeted(self._payload(targeted="junk"), limit=None)
        assert got == {"web1", "web2", "db1"}

    @pytest.mark.asyncio
    async def test_a_RETRY_can_never_widen_past_the_limit(self) -> None:
        """The consequence with teeth, asserted as CONTAINMENT rather than
        equality: whatever the remainder turns out to be, the `--limit` a retry
        carries must stay inside the one the operator asked for.

        Driven with nothing reported, which is the worst case -- every targeted
        host is outstanding, so this is the largest remainder possible.
        """
        asked_for = {"web1", "web2"}
        targeted = await self._targeted(
            self._payload(targeted={"limit": "web1:web2", "hosts": sorted(asked_for)}),
            limit="web1:web2",
        )
        outstanding = cr.outstanding_from(targeted, [])
        assert set(outstanding) <= asked_for, outstanding
        assert "db1" not in cr.remainder_limit(outstanding)
