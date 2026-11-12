"""The mint endpoint sizes a token to the Job it is for (#2526).

Two callers, and both have to be right:

* a listener current enough to say how long its Job needs sends a `ttl`, which
  is the authoritative number because the listener owns the Job's
  `activeDeadlineSeconds`;
* one that does not is an older image, and for a CONFIGURE the bare default is
  wrong by construction -- `token_ttl_seconds` is 3600 and a configure Job is
  allowed `configure_timeout_minutes` (180), so the token died an hour in and
  every upload at exit answered 401.

The clamp is unchanged and asserted here too, because a phase-sized default
that is not clamped is a way to express an unbounded runner token.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.api.routers import runs as router


def _cfg(**over):
    cfg = MagicMock()
    cfg.token_ttl_seconds = over.get("token_ttl_seconds", 3600)
    cfg.max_token_ttl_seconds = over.get("max_token_ttl_seconds", 14400)
    cfg.configure_timeout_minutes = over.get("configure_timeout_minutes", 180)
    cfg.termination_grace_period_seconds = over.get("termination_grace_period_seconds", 120)
    return cfg


async def _mint(*, status: str, body: dict, cfg: MagicMock | None = None):
    """Drive the endpoint and report the TTL the mint was handed.

    The TTL and not just the response, because `expires_in` is the CLAMPED
    figure: a test reading only that cannot tell a default that was sized
    correctly from one that was sized hugely and then cut back to the cap.
    """
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.listener_id = lid

    identity = MagicMock()
    identity.listener_id = lid

    captured: dict = {}

    def _gen(run_id, *, ttl, phase):
        captured["ttl"] = ttl
        captured["phase"] = phase
        return f"runtok:{run_id}:{phase or 'none'}"

    from terrapod.config import settings as live

    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch("terrapod.config.load_runner_config", return_value=cfg or _cfg()),
        patch("terrapod.auth.runner_tokens.generate_runner_token", _gen),
        patch.object(live.auth.oidc_issuer, "require_phase", False),
    ):
        resp = await router.create_runner_token(
            listener_id=f"listener-{lid}",
            run_id=f"run-{run.id}",
            body=body,
            identity=identity,
            db=MagicMock(),
        )
    import json

    return json.loads(resp.body), captured


class TestAListenerThatAsksGetsWhatItAsksFor:
    async def test_the_requested_ttl_reaches_the_mint(self):
        _resp, captured = await _mint(
            status="configuring", body={"phase": "configure", "ttl": 11220}
        )
        assert captured["ttl"] == 11220

    async def test_it_is_still_clamped_by_the_cap(self):
        """A phase-sized default that is not clamped is a way to express an
        unbounded runner token, which is the thing `_effective_max_ttl` exists
        to prevent."""
        _resp, captured = await _mint(
            status="configuring",
            body={"phase": "configure", "ttl": 999999},
            cfg=_cfg(max_token_ttl_seconds=7200),
        )
        # The route hands the request through; the clamp is the mint's, and the
        # response reports the clamped figure.
        assert captured["ttl"] == 999999
        resp, _ = await _mint(
            status="configuring",
            body={"phase": "configure", "ttl": 999999},
            cfg=_cfg(max_token_ttl_seconds=7200),
        )
        assert resp["expires_in"] == 7200


class TestAListenerThatDoesNotAsk:
    async def test_a_configure_is_sized_from_the_configure_ceiling(self):
        """The defect: this used to be `token_ttl_seconds`, 3600, against a Job
        allowed 180 minutes."""
        from terrapod.config import runner_token_ttl_for_job

        _resp, captured = await _mint(status="configuring", body={"phase": "configure"})
        assert captured["ttl"] == runner_token_ttl_for_job(180, 120)
        assert captured["ttl"] > 180 * 60

    async def test_a_longer_configured_default_is_not_shortened_by_the_sizing(self):
        """`max`, not a replacement: an operator who raised `tokenTTLSeconds`
        above the configure budget asked for that and must keep it."""
        _resp, captured = await _mint(
            status="configuring",
            body={"phase": "configure"},
            cfg=_cfg(token_ttl_seconds=50000),
        )
        assert captured["ttl"] == 50000

    async def test_a_plan_keeps_the_configured_default(self):
        """A plan's deadline is the Job builder's own 60 minutes, which lives in
        the listener and not here -- so sizing one here would restate a number
        held elsewhere. A current listener asks, which is the fix for a plan."""
        _resp, captured = await _mint(status="planning", body={"phase": "plan"})
        assert captured["ttl"] == 3600

    async def test_an_unphased_request_keeps_the_configured_default(self):
        """A listener predating the phase claim. It must still get a working
        token, and we cannot tell which phase to size for."""
        _resp, captured = await _mint(status="planning", body={})
        assert captured["ttl"] == 3600
        assert captured["phase"] is None


class TestTheReportedLifetimeMatchesTheMintedOne:
    async def test_a_non_positive_cap_reports_the_effective_cap(self):
        """`maxTokenTTLSeconds: 0` falls back to the shipped default at the
        mint. `expires_in` restated the old `if max_ttl > 0 else requested`
        rule, so it reported whatever was asked for while a clamped token was
        issued."""
        from terrapod.config import RunnerConfig

        resp, _captured = await _mint(
            status="planning",
            body={"phase": "plan", "ttl": 99999},
            cfg=_cfg(max_token_ttl_seconds=0),
        )
        assert resp["expires_in"] == RunnerConfig.model_fields["max_token_ttl_seconds"].default
