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

import inspect

from terrapod.api import app as app_module
from terrapod.auth import oidc_signing


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

    def test_it_fails_safe_rather_than_tearing_down_a_working_cache(self):
        """It raises BEFORE assigning `_keys`, so a transient database error on
        one interval leaves the previous working set in place. A refresh that
        blipped must never be the thing that stops a replica signing."""
        source = inspect.getsource(oidc_signing.reload_signing_keys)
        raise_at = source.index('raise RuntimeError("No live OIDC issuer signing key')
        assign_at = source.index("_keys = [SigningKey(")
        assert raise_at < assign_at, (
            "reload_signing_keys assigns before it validates, so a bad interval "
            "would replace a working cache with a broken one"
        )

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
