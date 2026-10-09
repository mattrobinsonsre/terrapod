"""The phase bound into a runner token must not be the listener's to choose.

`apply` is what resolves to the extra apply cloud identity (#1901), and the
whole point of the base/apply split is that a pull-request plan -- whose HCL
the PR author wrote -- cannot reach write permissions. The mint endpoint takes
the phase from the presented token rather than its own request body, which is
right; but one hop earlier the token's phase came from the listener's request
body, membership-checked against {plan, apply} and otherwise unverified.

So the guarantee rested on the listener being honest about a field it chooses,
while the server held the authoritative answer in `run.status` all along. The
listener is in fact only echoing back the phase the API gave it on the claim.

There was no test for this endpoint at all before these.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as router


def _listener(lid: uuid.UUID):
    ident = MagicMock()
    ident.listener_id = lid
    return ident


async def _call(*, status: str, body: dict, lid: uuid.UUID | None = None):
    lid = lid or uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.listener_id = lid

    db = MagicMock()
    cfg = MagicMock()
    cfg.token_ttl_seconds = 3600
    cfg.max_token_ttl_seconds = 7200

    captured: dict = {}

    def _gen(run_id, *, ttl, phase):
        captured["phase"] = phase
        return f"runtok:{run_id}:{phase or 'none'}"

    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch("terrapod.config.load_runner_config", return_value=cfg),
        patch("terrapod.auth.runner_tokens.generate_runner_token", _gen),
    ):
        resp = await router.create_runner_token(
            listener_id=f"listener-{lid}",
            run_id=f"run-{run.id}",
            body=body,
            listener=_listener(lid),
            db=db,
        )
    return resp, captured


class TestAListenerCannotPromoteItsOwnPhase:
    @pytest.mark.parametrize("status", ["planning", "planned", "queued", "pending"])
    async def test_an_apply_claim_on_a_non_applying_run_is_downgraded(self, status):
        """The attack shape: a listener launching a plan Job asks for `apply`,
        and the run then draws the apply cloud identity at plan time."""
        _resp, captured = await _call(status=status, body={"phase": "apply"})
        assert captured["phase"] == "plan"

    @pytest.mark.parametrize("status", ["confirmed", "applying"])
    async def test_a_genuine_apply_phase_still_gets_it(self, status):
        """The negative path. These are the states an apply Job is launched
        from, and a plan-only run never reaches either -- which is what makes
        the check usable rather than merely strict."""
        _resp, captured = await _call(status=status, body={"phase": "apply"})
        assert captured["phase"] == "apply"

    async def test_a_plan_claim_is_untouched(self):
        _resp, captured = await _call(status="planning", body={"phase": "plan"})
        assert captured["phase"] == "plan"

    async def test_an_absent_phase_still_mints_the_unphased_form(self):
        """Load-bearing for listener skew: an image older than the claim sends no
        phase and must still get a working token, never a 4xx."""
        _resp, captured = await _call(status="planning", body={})
        assert captured["phase"] is None

    async def test_an_unrecognised_phase_mints_the_unphased_form(self):
        _resp, captured = await _call(status="applying", body={"phase": "destroy"})
        assert captured["phase"] is None
