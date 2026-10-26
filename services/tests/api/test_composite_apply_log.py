"""The composite's configure is concatenated onto the apply log (#1988).

The platform has three phases and the CLI sees two. `go-tfe` switches on a
fixed status set with no `configuring` in it, so the only shape the existing
clients can represent is the configure arriving on the apply's own stream --
which is the user's own framing on the issue: "concatenate the log stream and
the final result ... onto the apply phase".

**Every test here exists because the live proof found the opposite.** Driving a
real `tofu apply` through a composite on the Tilt stack, the CLI stayed attached
for the configure's whole duration and returned the composite result -- and its
stream carried ZERO ansible bytes: no `PLAY RECAP`, no `TASK [`, in a 2854-byte
log. #1988's mechanism argument was right about the result and silent about the
bytes, because `_serve_log`'s terminality rule keeps the stream OPEN without
putting anything into it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as mod

APPLY = b"Apply complete! Resources: 1 added, 0 changed, 0 destroyed.\n"
CONFIGURE = b"PLAY RECAP ****\nweb1 : ok=5 changed=2\n"


def _run(*, is_configure: bool, status: str = "applied"):
    run = MagicMock()
    run.id = "11111111-1111-4111-8111-111111111111"
    run.workspace_id = "22222222-2222-4222-8222-222222222222"
    run.status = status
    run.is_configure = is_configure
    return run


async def _serve(run, *, tail, phase_bytes):
    """Drive `_serve_log` with storage answering the apply and `_phase_bytes`
    answering the tail, which is the seam the concatenation hangs on."""
    storage = MagicMock()
    storage.get = AsyncMock(return_value=APPLY)
    with (
        patch.object(mod, "get_storage", return_value=storage),
        patch.object(mod, "_phase_bytes", AsyncMock(return_value=phase_bytes)),
    ):
        resp = await mod._serve_log(
            run=run,
            log_key="logs/w/applies/r.log",
            phase_complete_states=frozenset({"applied", "errored", "discarded", "canceled"}),
            offset=0,
            limit=0,
            phase="apply",
            tail=tail,
        )
    return resp.body


class TestTheConfigureArrivesOnTheApplyStream:
    @pytest.mark.asyncio
    async def test_a_composite_carries_both(self):
        body = await _serve(
            _run(is_configure=True),
            tail=("logs/w/configures/r.log", "configure"),
            phase_bytes=CONFIGURE,
        )
        assert b"Apply complete!" in body
        assert b"PLAY RECAP" in body, (
            "the configure's bytes are absent from the apply stream -- which is "
            "exactly what the live proof found before this existed"
        )
        # Order matters: the apply's bytes are complete before the configure
        # starts, so they must come FIRST or a polling client's consumed prefix
        # changes length under it.
        assert body.index(b"Apply complete!") < body.index(b"PLAY RECAP")

    @pytest.mark.asyncio
    async def test_a_terraform_only_run_is_byte_identical(self):
        """The property #1988 requires of every terraform/tofu deployment:
        nothing here is reachable without a configure, and that is pinned rather
        than asserted. `tail=None` is what the route passes when
        `run.is_configure` is false."""
        body = await _serve(_run(is_configure=False), tail=None, phase_bytes=CONFIGURE)
        assert b"PLAY RECAP" not in body
        assert APPLY in body

    @pytest.mark.asyncio
    async def test_the_host_tag_is_stripped_from_the_tail(self):
        """The operator's terminal gets ansible's words, not our framing.

        Measured on the live proof: the CLI showed `~          | TASK [...]`,
        because the strip was keyed on `format=plain` and the cloud backend
        never asks for it. The prefix belongs to the configure tab's per-host
        picker; the apply stream has no picker to serve.
        """
        tagged = mod._HOST_LOG_HEADER + b"\n~          | TASK [something]\n"
        body = await _serve(
            _run(is_configure=True),
            tail=("logs/w/configures/r.log", "configure"),
            phase_bytes=tagged,
        )
        assert b"TASK [something]" in body
        assert b"~          | " not in body

    @pytest.mark.asyncio
    async def test_a_terminal_run_whose_tail_has_not_landed_does_not_close(self):
        """The ETX hazard, and it needs a TERMINAL run to exist at all.

        A first version of this test used `status="configuring"`, where
        `phase_done` is already false and so ETX could not fire whatever the
        guard did -- it passed against the mutation that deletes the guard,
        which is how it was caught. The case the guard is actually for: the run
        has reached `applied`, so `phase_done` is TRUE and the apply's bytes are
        in storage, but the configure's upload has not landed yet. Without the
        guard the sentinel fires on the apply alone, a polling client stops, and
        the operator sees the apply finish and never learns a configure ran.
        """
        body = await _serve(
            _run(is_configure=True, status="applied"),
            tail=("logs/w/configures/r.log", "configure"),
            phase_bytes=None,
        )
        assert mod._ETX not in body

    @pytest.mark.asyncio
    async def test_a_complete_composite_does_close_the_stream(self):
        """The positive control. Without it the test above passes against a
        route that never sends ETX at all, which would hang every client."""
        body = await _serve(
            _run(is_configure=True),
            tail=("logs/w/configures/r.log", "configure"),
            phase_bytes=CONFIGURE,
        )
        assert mod._ETX in body


class TestTheRouteDecidesWhetherThereIsATail:
    """Mutation target: passing the tail for EVERY run.

    The class above drives `_serve_log` with `tail=None` supplied by hand, so it
    says nothing about the route choosing it -- a presence check on the
    function, not an application check on the caller. Make the route pass a tail
    unconditionally and those tests still pass, while every terraform-only
    deployment starts paying a storage read and a Redis read per log poll for a
    phase it does not have.
    """

    async def _drive(self, run):
        """Through the apply-log route, which is where the decision is made."""
        seen: list = []

        async def _spy(_run, key, phase):
            seen.append((key, phase))
            return None

        storage = MagicMock()
        storage.get = AsyncMock(return_value=APPLY)
        with (
            patch.object(mod, "get_storage", return_value=storage),
            patch.object(mod, "_phase_bytes", _spy),
            patch.object(
                mod,
                "resolve_capability_or_authenticate",
                AsyncMock(return_value=("run-" + str(run.id), None)),
            ),
            patch.object(mod.run_service, "get_run", AsyncMock(return_value=run)),
            patch.object(mod, "parse_id", lambda *a, **k: run.id),
        ):
            await mod.apply_log(
                request=MagicMock(),
                apply_id="apply-x",
                offset=0,
                limit=0,
                format="raw",
                db=AsyncMock(),
            )
        return seen

    @pytest.mark.asyncio
    async def test_a_configure_run_gets_one(self):
        seen = await self._drive(_run(is_configure=True))
        assert [p for _, p in seen] == ["configure"], f"route asked for {seen}"

    @pytest.mark.asyncio
    async def test_a_terraform_only_run_gets_none(self):
        seen = await self._drive(_run(is_configure=False))
        assert seen == [], (
            "the route asked for a configure log on a run that has no configure "
            f"-- {seen}. A terraform-only deployment must pay nothing for this."
        )


class TestAppliedAtMeansTheRunReachedApplied:
    """Mutation target: reverting `applied-at` to `apply_finished_at`.

    go-tfe's `RunStatusTimestamps` is a struct of per-STATUS times --
    `AppliedAt`, `ApplyingAt`, `PlannedAt` -- so a client reads `applied-at` as
    the run's completion. The composite hand-over sets `apply_finished_at` when
    the apply YIELDS to the configure, which on a measured live run reported
    completion 40.5s early; #1977 exists because a configure can run for hours,
    so the error is unbounded rather than small.

    Nothing covered this until the mutation killed zero tests.
    """

    def _run(self, **kw):
        run = MagicMock()
        run.id = "33333333-3333-4333-8333-333333333333"
        run.workspace_id = "44444444-4444-4444-8444-444444444444"
        run.status = "applied"
        for attr, value in {
            "plan_started_at": None,
            "plan_finished_at": None,
            "apply_started_at": None,
            "apply_finished_at": None,
            "configure_started_at": None,
            "configure_finished_at": None,
        }.items():
            setattr(run, attr, value)
        for k, v in kw.items():
            setattr(run, k, v)
        return run

    def _stamps(self, run, *, tfe):
        with patch.object(mod, "_rfc3339", lambda d: d.isoformat() + "Z" if d else None):
            out = mod._run_json(run, engine="terraform", tfe=tfe)
        return out["data"]["attributes"]["status-timestamps"]

    @pytest.mark.asyncio
    async def test_a_composite_reports_the_configure_s_end(self):
        from datetime import UTC, datetime

        applied = datetime(2026, 10, 26, 16, 8, 16, tzinfo=UTC)
        configured = datetime(2026, 10, 26, 16, 8, 57, tzinfo=UTC)
        s = self._stamps(
            self._run(apply_finished_at=applied, configure_finished_at=configured),
            tfe=False,
        )
        assert s["applied-at"].startswith("2026-10-26T16:08:57"), (
            f"applied-at is {s['applied-at']} -- the APPLY's end, 41s before the "
            "run reached `applied`"
        )

    @pytest.mark.asyncio
    async def test_a_terraform_only_run_is_unchanged(self):
        """The two are the same instant without a configure, because
        `transition_run` sets `apply_finished_at` on the transition INTO
        `applied` -- which is why this is not a breaking change."""
        from datetime import UTC, datetime

        applied = datetime(2026, 10, 26, 16, 8, 16, tzinfo=UTC)
        s = self._stamps(self._run(apply_finished_at=applied), tfe=True)
        assert s["applied-at"].startswith("2026-10-26T16:08:16")

    @pytest.mark.asyncio
    async def test_the_configure_times_are_native_only(self):
        """Same fold as `configuring`->`applying`: the honest detail on
        `/api/v1`, the compatible shape on the frozen surface."""
        from datetime import UTC, datetime

        run = self._run(
            apply_finished_at=datetime(2026, 10, 26, 16, 8, 16, tzinfo=UTC),
            configure_started_at=datetime(2026, 10, 26, 16, 8, 38, tzinfo=UTC),
            configure_finished_at=datetime(2026, 10, 26, 16, 8, 57, tzinfo=UTC),
        )
        native = self._stamps(run, tfe=False)
        frozen = self._stamps(run, tfe=True)
        assert "configuring-at" in native and "configured-at" in native
        assert "configuring-at" not in frozen and "configured-at" not in frozen
