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
    cert_lid: uuid.UUID | None = None,
    run_lid: uuid.UUID | None = None,
):
    """Drive the endpoint.

    `cert_lid` is the listener the CALLER'S CERTIFICATE names, defaulting to the
    one in the path. They are separate parameters because the whole of
    GHSA-g4j5 is that the endpoint compared the path against the run and never
    against the certificate, so a test that cannot make them differ cannot see
    it.

    `run_lid` is the listener the RUN belongs to, defaulting to the one in the
    path. It exists for the ordering test: with the run's owner pinned to the
    path, the ownership check can never fire, so swapping the two checks over
    produces the same message and the test cannot see the order it asserts.
    Three distinct values are what make both checks live at once.
    """
    lid = lid or uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.listener_id = run_lid if run_lid is not None else lid

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
            identity=_listener(cert_lid or lid),
            db=db,
        )
    return resp, captured


class TestAListenerCannotPromoteItsOwnPhase:
    async def test_an_apply_claim_on_a_PLANNING_run_is_downgraded(self):
        """The attack shape: a listener launching a plan Job asks for `apply`,
        and the run then draws the apply cloud identity at plan time.

        Downgraded rather than refused, because the run IS executing a plan and
        failing the token would fail the run instead of correcting the listener.
        Narrowed from a four-status parametrisation: the other three are runs
        executing nothing at all, and those are now refused outright -- see
        `TestATokenIsOnlyIssuedWhileThePhaseIsLIVE`. The ordering is what makes
        both true, and it is asserted there.
        """
        _resp, captured = await _call(status="planning", body={"phase": "apply"})
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


class TestTheCALLERSCERTIFICATEMustNameTheListenerInThePath:
    """GHSA-g4j5. The endpoint resolved the certificate and never read it.

    `get_listener_identity` authenticated the caller, the result was bound to a
    parameter typed `object`, and the only ownership check compared
    `run.listener_id` against the **path** segment -- which the caller chooses.
    Every sibling listener endpoint compares the certificate too.

    So a holder of any valid listener certificate, from ANY pool, could name
    another pool's listener and one of its runs and be handed a runner token for
    it: artifact and state write access, and since per-workspace cloud identity
    the target workspace's federated identity with real `workspace` and `phase`
    claims that no cloud-side trust policy can distinguish from a legitimate
    token. Compromise of a low-trust pool reached a high-trust pool's apply
    identity.
    """

    async def test_a_certificate_for_another_listener_is_refused(self):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={"phase": "plan"}, lid=theirs, cert_lid=mine)
        assert exc.value.status_code == 403
        assert "Certificate does not match" in exc.value.detail

    async def test_the_matching_certificate_still_works(self):
        """The contrast. A refusal that caught the legitimate caller would take
        down every run in the estate, which is worse than the finding."""
        _resp, captured = await _call(status="planning", body={"phase": "plan"})
        assert captured["phase"] == "plan"

    async def test_the_certificate_is_checked_BEFORE_the_run_ownership(self):
        """Order matters for what the attacker learns. Checking the run first
        would answer "Run not assigned to this listener" for a path naming a
        listener the caller has no certificate for -- which confirms whether that
        listener owns that run, to a caller who should not get that far."""
        mine, theirs, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # All three differ, so BOTH checks would fire and only their order
        # decides which message comes back. With the run owned by `theirs` --
        # as this test used to have it -- the ownership check cannot fire, and
        # the assertion below passes under a deliberate reordering.
        with pytest.raises(HTTPException) as exc:
            await _call(
                status="planning",
                body={"phase": "plan"},
                lid=theirs,
                cert_lid=mine,
                run_lid=owner,
            )
        assert "Certificate does not match" in exc.value.detail
        assert "Run not assigned" not in exc.value.detail

    async def test_the_ownership_check_is_reached_when_the_certificate_matches(self):
        """The other half, or the test above could pass with no ownership check
        at all. A caller holding the right certificate for the path, naming a
        run that belongs to someone else, must still be refused."""
        mine, owner = uuid.uuid4(), uuid.uuid4()
        with pytest.raises(HTTPException) as exc:
            await _call(
                status="planning",
                body={"phase": "plan"},
                lid=mine,
                cert_lid=mine,
                run_lid=owner,
            )
        assert "Certificate does not match" not in exc.value.detail

    def test_the_parameter_is_typed_like_every_sibling(self):
        """The weak type is why the missing check was invisible.

        It was `listener: object`, so a reader never saw that `.listener_id` was
        available to compare -- the parameter read as an opaque handle. Typed as
        `ListenerIdentity` this endpoint is indistinguishable from its siblings,
        which is what matters: the next listener endpoint gets written by copying
        one of them.
        """
        import inspect

        from terrapod.api.dependencies import ListenerIdentity

        params = inspect.signature(router.create_runner_token).parameters
        assert "identity" in params, "the certificate parameter was renamed again"
        assert params["identity"].annotation is ListenerIdentity


class TestATokenIsOnlyIssuedWhileThePhaseIsLIVE:
    """GHSA-p9jh. `TERMINAL_STATES` was the wrong set.

    A plan-only run never reaches a terminal state: it finishes in `planned` and
    stays there. Speculative pull-request plans, module-impact plans and drift
    runs are all plan-only -- and are precisely the runs executing the
    least-trusted code, an unmerged branch. So the liveness check never engaged
    for them, and fresh tokens could be issued for a finished plan indefinitely.
    """

    @pytest.mark.parametrize("status", ["planned", "queued", "pending", "applied"])
    async def test_a_run_executing_nothing_is_refused(self, status):
        with pytest.raises(HTTPException) as exc:
            await _call(status=status, body={"phase": "plan"})
        assert exc.value.status_code == 409
        assert "not executing" in exc.value.detail

    async def test_the_message_names_the_plan_only_case(self):
        """`planned` is the state a plan-only run FINISHES in, so an operator
        reading this needs to know that is the point rather than a transient."""
        with pytest.raises(HTTPException) as exc:
            await _call(status="planned", body={"phase": "plan"})
        assert "plan-only" in exc.value.detail

    async def test_a_planning_run_gets_a_plan_token(self):
        _resp, captured = await _call(status="planning", body={"phase": "plan"})
        assert captured["phase"] == "plan"

    @pytest.mark.parametrize("status", ["confirmed", "applying"])
    async def test_an_apply_phase_run_gets_an_apply_token(self, status):
        """`confirmed` is kept deliberately. The claim is atomic, so in every
        observed path the state is `applying` by the time a token is asked for --
        but this ships in a patch, and if some path does ask at `confirmed` the
        tighter rule would fail every apply on it while the looser one closes the
        finding just as completely."""
        _resp, captured = await _call(status=status, body={"phase": "apply"})
        assert captured["phase"] == "apply"

    async def test_the_downgrade_runs_BEFORE_the_liveness_check(self):
        """Both fixes have to hold at once, and the order is what allows it.

        A listener asking `apply` on a `planning` run is corrected to `plan` and
        then issued, because the run is executing a plan. Liveness-first on the
        REQUESTED phase would refuse it -- apply is not live in `planning` -- and
        that is the legitimate path every plan Job takes when a listener
        mis-reports.
        """
        _resp, captured = await _call(status="planning", body={"phase": "apply"})
        assert captured["phase"] == "plan"


class TestTheRequestedTTLIsValidated:
    """#2044. `ttl` came straight off the body.

    A non-integer reached `ttl > max_ttl` inside the minting helper and answered
    500 on a TypeError; a negative value sailed past that same comparison and
    minted an already-expired token, which fails the run with an authentication
    error rather than a cause.
    """

    @pytest.mark.parametrize("bad", ["3600", None, 1.5, [], {}])
    async def test_a_non_integer_is_a_422_not_a_500(self, bad):
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={"phase": "plan", "ttl": bad})
        assert exc.value.status_code == 422
        assert "must be an integer" in exc.value.detail

    async def test_a_bool_is_refused_too(self):
        """`bool` is a subclass of `int`, so an isinstance check alone would let
        `ttl: true` through and mint a one-second token."""
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={"phase": "plan", "ttl": True})
        assert exc.value.status_code == 422

    @pytest.mark.parametrize("bad", [0, -1, -3600])
    async def test_a_non_positive_ttl_is_refused(self, bad):
        with pytest.raises(HTTPException) as exc:
            await _call(status="planning", body={"phase": "plan", "ttl": bad})
        assert exc.value.status_code == 422
        assert "at least 1 second" in exc.value.detail

    async def test_a_valid_ttl_is_accepted(self):
        """The contrast, and the shape every real listener sends."""
        _resp, captured = await _call(status="planning", body={"phase": "plan", "ttl": 600})
        assert captured["phase"] == "plan"

    async def test_an_absent_ttl_takes_the_configured_default(self):
        _resp, captured = await _call(status="planning", body={"phase": "plan"})
        assert captured["phase"] == "plan"
