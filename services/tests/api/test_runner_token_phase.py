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
from fastapi import HTTPException

from terrapod.api.routers import runs as router


def _listener(lid: uuid.UUID):
    ident = MagicMock()
    ident.listener_id = lid
    return ident


async def _call(
    *,
    status: str,
    body: dict,
    lid: uuid.UUID | None = None,
    require_phase: bool = False,
):
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

    # Patched on the live singleton rather than replaced wholesale: the route
    # reads one attribute off it and a MagicMock settings object would make
    # `require_phase` truthy in every test that did not think about it.
    from terrapod.config import settings as live

    with (
        patch.object(router, "_get_run", AsyncMock(return_value=run)),
        patch("terrapod.config.load_runner_config", return_value=cfg),
        patch("terrapod.auth.runner_tokens.generate_runner_token", _gen),
        patch.object(live.auth.oidc_issuer, "require_phase", require_phase),
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


class TestRequirePhaseClosesOffTheUnphasedForm:
    """`require_phase` refuses the unphased token outright (#2041).

    Refused HERE as well as at the mint, and this is the better of the two
    places to meet it: the listener sees the error and the run fails with a
    cause it can report, where a refusal at the mint surfaces as the credential
    phase failing for a reason the listener never saw.
    """

    async def test_an_absent_phase_is_refused_when_it_is_set(self):
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={}, require_phase=True)
        assert exc.value.status_code == 400
        assert "require_phase" in exc.value.detail
        assert "upgrade the listener" in exc.value.detail.lower()

    async def test_an_unrecognised_phase_is_refused_too(self):
        """It is read as "makes no claim", so it reaches the same form and must
        meet the same refusal -- otherwise `phase: "APPLY"` is a way round the
        setting."""
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={"phase": "configure"}, require_phase=True)
        assert exc.value.status_code == 400

    async def test_a_named_phase_is_unaffected(self):
        """The contrast. A refusal that caught named phases too would fail every
        run on a deployment that set this."""
        _, cap = await _call(status="planning", body={"phase": "plan"}, require_phase=True)
        assert cap["phase"] == "plan"

    async def test_a_DOWNGRADED_apply_claim_still_passes(self):
        """The order matters, and this is the case that shows it. An `apply`
        claim on a non-applying run is downgraded to `plan`, not to None, so it
        must satisfy the requirement -- checking before the downgrade would
        refuse a run the server had already corrected."""
        _, cap = await _call(status="planning", body={"phase": "apply"}, require_phase=True)
        assert cap["phase"] == "plan"

    async def test_an_absent_phase_still_mints_when_it_is_NOT_set(self):
        """The default, and it has to keep working: a listener predating the
        phase claim is the only thing that sends none, and an upgrade must not
        strand it."""
        _, cap = await _call(status="planning", body={})
        assert cap["phase"] is None
