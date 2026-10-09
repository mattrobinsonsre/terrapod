"""A rotation has to reach every replica, and nothing was making it (#1901).

`_keys` and `_signing_kid` are module globals in `auth/oidc_signing.py`;
`get_signing_key()` and `get_jwks()` read them without touching the database.
So the only thing that can propagate a rotation is something that calls
`reload_signing_keys` again — and before this guard existed its only caller was
`rotate_signing_key` itself, on the one replica that served the request.

Two consequences, and both are invisible from the Terrapod side because the
failure lands at the cloud's token exchange:

* the published JWKS differed by pod behind a load balancer, so a cloud got the
  new `kid` or not depending on which replica answered;
* `_signing_kid` is a point-in-time choice. At rotation `_choose_signing_kid`
  correctly picks the RETIRED key, because the new one does not activate until
  `key_propagation_seconds` has passed. Nothing recomputed it, so the handover
  the rotation design exists for never happened without a restart — while the
  runbook told the operator to confirm that it had.

`_choose_signing_kid`'s three tiers are already well covered in
`test_oidc_signing.py`. What was missing is that anything calls it again, which
is a wiring property rather than a behavioural one, so it is asserted against
the source the way the replication-startup gates are.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api import app as app_module
from terrapod.auth import oidc_signing
from terrapod.config import settings


class TestTheRefreshIsWired:
    def test_a_periodic_task_reloads_the_signing_keys(self):
        source = inspect.getsource(app_module)
        assert '"oidc_signing_refresh"' in source, (
            "no periodic task re-reads the OIDC signing keys, so a rotation "
            "reaches only the replica that served it and the propagation-window "
            "handover never happens"
        )

    def test_the_registered_handler_is_the_refresh_cycle(self):
        """Pins the WIRING; the behaviour is pinned behaviourally elsewhere.

        Two earlier versions of this test read `app.py`'s source. The first
        asserted the handler's NAME and survived deleting the call, because the
        handler's own `from ... import reload_signing_keys` keeps the substring
        alive. The second asserted the awaited call inside an inline closure --
        and broke when the handler became a named function in `oidc_signing.py`,
        which is where every other one of this file's twenty periodic handlers
        already lives. The behaviour had not changed at all.

        That is the limit of a source-text assertion: it tracks where the code
        is written rather than what it does. So this one checks only the thing
        source can answer honestly -- that the registration names the cycle
        function -- and `tests/api/test_oidc_issuer_recovery.py` executes that
        function to pin what it actually does, including that it INITIALISES
        when nothing is loaded rather than only re-reading.
        """
        source = inspect.getsource(app_module)
        reg = source.index('"oidc_signing_refresh"')
        window = source[reg : reg + 400]
        assert "handler=refresh_signing_keys_cycle" in window, (
            "the periodic task does not register refresh_signing_keys_cycle, so "
            "either a rotation never reaches other replicas or a failed startup "
            "never recovers"
        )

    def test_it_is_gated_on_the_issuer_being_enabled(self):
        """A deployment that publishes no issuer must not pay for a database
        round trip every interval, and `reload_signing_keys` would raise on a
        deployment with no keys at all."""
        source = inspect.getsource(app_module)
        reg = source.index('"oidc_signing_refresh"')
        gate = source.rindex("if settings.auth.oidc_issuer.enabled:", 0, reg)
        assert gate > 0, "oidc_signing_refresh must be registered under the issuer gate"
        # Exactly one registration between the gate and this task -- its own. A
        # second would mean the gate had been reused for something unrelated,
        # which silently changes when that other task runs.
        assert source[gate:reg].count("register_periodic_task") == 1


class TestTheReloadIsSafeToRunOnATimer:
    """Three properties the periodic caller depends on, none of which the
    rotation path needed when it was the only caller."""

    def _reset(self):
        oidc_signing._reset_for_tests()

    def _load_a_working_key(self):
        """Put a real, working set in the cache, as a healthy replica holds."""
        key = oidc_signing.generate_private_key()
        kid = oidc_signing.compute_kid(key)
        oidc_signing._keys = [
            oidc_signing.SigningKey(
                kid=kid,
                private_key_pem=oidc_signing.serialize_private_key(key),
                row_id=uuid.uuid4(),
            )
        ]
        oidc_signing._signing_kid = kid
        oidc_signing._jwks_cache = None
        return kid

    def _db(self, rows=None, *, raises=None):
        scalars = MagicMock()
        scalars.all.return_value = rows or []
        result = MagicMock()
        result.scalars.return_value = scalars

        class _DB:
            async def execute(self, *a, **k):
                if raises is not None:
                    raise raises
                return result

            async def commit(self):
                return None

        return _DB()

    def _stale_row(self, grace):
        row = MagicMock()
        row.kid = "LONG-RETIRED"
        row.id = uuid.uuid4()
        row.private_key_pem = "irrelevant"
        row.retired_at = datetime.now(UTC) - timedelta(seconds=grace + 3600)
        row.created_at = datetime.now(UTC) - timedelta(days=90)
        row.activates_at = row.created_at
        return row

    def test_a_reload_that_finds_nothing_live_leaves_the_working_cache_in_PLACE(self):
        """Behavioural, replacing a source-order assertion.

        That assertion compared the index of the `raise` literal against the
        index of `_keys = [SigningKey(`, which is satisfied by any arrangement
        that merely *spells* the raise above the assignment — moving the
        validation into a nested helper whose `raise` sits textually first but
        executes last passes it while the cache is torn down exactly as before.

        The property is what matters: this runs every 30s on every replica, so a
        reload that found nothing live and committed that answer would make a
        transient database state the thing that stops a replica signing. Pinned
        by the observable consequence — `get_jwks` and `get_signing_key` still
        answer with the set that was working.
        """
        self._reset()
        try:
            kid = self._load_a_working_key()
            before = oidc_signing.get_jwks()
            grace = settings.auth.oidc_issuer.retired_key_grace_seconds

            with patch.object(oidc_signing, "_configured_key_pem", return_value=None):
                with pytest.raises(RuntimeError, match="No live OIDC issuer signing key"):
                    asyncio.run(
                        oidc_signing.reload_signing_keys(self._db([self._stale_row(grace)]))
                    )

            assert oidc_signing.get_signing_key().kid == kid
            assert oidc_signing.get_jwks() == before
            assert [k["kid"] for k in oidc_signing.get_jwks()["keys"]] == [kid]
        finally:
            self._reset()

    def test_a_database_error_mid_reload_also_leaves_the_cache_in_place(self):
        """The failure the docstring actually names. It propagates from the
        query, before anything is assigned, so a replica whose database blipped
        on one interval keeps signing and the next interval recovers."""
        self._reset()
        try:
            kid = self._load_a_working_key()
            before = oidc_signing.get_jwks()

            with patch.object(oidc_signing, "_configured_key_pem", return_value=None):
                with pytest.raises(OSError, match="connection reset"):
                    asyncio.run(
                        oidc_signing.reload_signing_keys(
                            self._db(raises=OSError("connection reset"))
                        )
                    )

            assert oidc_signing.get_signing_key().kid == kid
            assert oidc_signing.get_jwks() == before
        finally:
            self._reset()

    def test_a_healthy_reload_does_converge_so_the_two_above_are_not_vacuous(self):
        """Both tests above assert that nothing changed, which a `reload` that
        did nothing at all would also satisfy. This one proves a reload really
        does replace the set — a rotation on another replica reaching this one
        is the whole reason the timer exists."""
        self._reset()
        try:
            self._load_a_working_key()

            new = oidc_signing.generate_private_key()
            new_kid = oidc_signing.compute_kid(new)
            row = MagicMock()
            row.kid = new_kid
            row.id = uuid.uuid4()
            row.private_key_pem = oidc_signing.serialize_private_key(new)
            row.retired_at = None
            row.created_at = datetime.now(UTC)
            row.activates_at = datetime.now(UTC) - timedelta(seconds=1)

            with patch.object(oidc_signing, "_configured_key_pem", return_value=None):
                asyncio.run(oidc_signing.reload_signing_keys(self._db([row])))

            assert oidc_signing.get_signing_key().kid == new_kid
            assert [k["kid"] for k in oidc_signing.get_jwks()["keys"]] == [new_kid]
        finally:
            self._reset()

    def test_it_returns_early_for_an_operator_supplied_key(self):
        """A BYO deployment holds a key that is not in the database at all, so a
        reload must not look for one — it would find nothing live and raise on
        every interval."""
        source = inspect.getsource(oidc_signing.reload_signing_keys)
        early = source.index("_configured_key_pem() is not None")
        query = source.index("select(OIDCSigningKey)")
        assert early < query

    def test_it_recomputes_the_signing_choice_not_just_the_published_set(self):
        """The published set converging is not enough: the whole point is that
        `_signing_kid` flips to the new key once its activation time has passed.
        A reload that refreshed `_keys` alone would leave the handover broken
        while looking fixed."""
        source = inspect.getsource(oidc_signing.reload_signing_keys)
        assert "_choose_signing_kid" in source


class TestTheRefreshHandlerItselfRuns:
    """The handler, executed rather than read as text.

    Everything above this point inspects `inspect.getsource`, because the
    REGISTRATION happens inside `lifespan` and reaching it the ordinary way
    means starting the whole application -- database, Redis, storage,
    connectors, scheduler. The handler itself needs none of that: it is a
    module-level function, so it is simply called here, and deleting the
    `await reload_signing_keys(db)` line fails these because nothing was
    called, not because a substring moved.

    It was a closure inside `lifespan` when these tests were first written, and
    reaching it then meant rebuilding its code object over the module globals.
    Moving it out (#2026, so the task could also RECOVER a failed startup by
    generating a key, which only `init_oidc_signing` does) removed the need --
    and the reconstruction's own guard is what reported the move, loudly, which
    is the behaviour it was written for.

    What is asserted here and nowhere else is the POOL. The branch itself --
    reload when a set is loaded, initialise when not -- belongs to
    `test_oidc_issuer_recovery.py`, which patches the session wholesale and so
    says nothing about which session mechanism the handler uses.
    """

    def test_it_awaits_reload_signing_keys_with_a_session_of_its_own(self):
        """The call, and the SESSION.

        `get_db_session` is an async context manager, not the request-scoped
        `get_db` dependency: a handler reaching for `get_db` would hold a
        connection for the life of the task rather than for the length of the
        query, which on a 30-second timer across every replica is how a pool
        gets exhausted. Asserted by identity on the object the context manager
        yielded, so the session the query received is the one it opened.
        """
        session = object()

        @asynccontextmanager
        async def fake_session():
            yield session

        with (
            patch("terrapod.db.session.get_db_session", fake_session),
            patch.object(oidc_signing, "_keys", [MagicMock(kid="k1")]),
            patch.object(oidc_signing, "reload_signing_keys", new_callable=AsyncMock) as reload,
        ):
            asyncio.run(oidc_signing.refresh_signing_keys_cycle())

        reload.assert_awaited_once()
        assert reload.await_args.args[0] is session

    def test_the_session_is_released_even_when_the_reload_raises(self):
        """`reload_signing_keys` raises by design when it finds nothing live, and
        this runs every 30s -- so a handler that leaked the session on that path
        would exhaust the pool within the hour on exactly the deployment already
        in trouble.
        """
        closed = []

        @asynccontextmanager
        async def fake_session():
            try:
                yield object()
            finally:
                closed.append(True)

        with (
            patch("terrapod.db.session.get_db_session", fake_session),
            patch.object(oidc_signing, "_keys", [MagicMock(kid="k1")]),
            patch.object(
                oidc_signing,
                "reload_signing_keys",
                new_callable=AsyncMock,
                side_effect=RuntimeError("No live OIDC issuer signing key"),
            ),
        ):
            with pytest.raises(RuntimeError, match="No live OIDC issuer signing key"):
                asyncio.run(oidc_signing.refresh_signing_keys_cycle())

        assert closed == [True], "the session was not released on the raising path"

    def test_the_handler_is_module_level_so_the_two_above_run_the_shipped_code(self):
        """Pins what makes the direct call legitimate.

        If it is ever nested back inside `lifespan`, calling
        `oidc_signing.refresh_signing_keys_cycle` would stop reaching the
        registered handler and the two tests above would quietly be about a
        different function. `app.py` reads it from this module, so that import
        is the coupling and it is asserted rather than assumed.
        """
        assert inspect.iscoroutinefunction(oidc_signing.refresh_signing_keys_cycle)
        assert "_oidc_signing_refresh" not in inspect.getsource(app_module), (
            "the handler is nested in `lifespan` again -- the two tests above call "
            "the module-level function and would no longer be testing what runs"
        )
        registration = inspect.getsource(app_module)
        assert "from terrapod.auth.oidc_signing import refresh_signing_keys_cycle" in registration
        assert "handler=refresh_signing_keys_cycle" in registration
