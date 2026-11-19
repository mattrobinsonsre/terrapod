"""Telling an operator that an artifact was refused (#2589, decision 2).

The decision: *a verification failure or an allowlist refusal is a real event
with a meaning, and notification configurations are the channel operators
already have wired.*

Two properties carry the weight here, and both fail quietly if broken:

- **a refusal is never swallowed by the attempt to announce it.** The artifact
  was correctly refused; a notification problem turning that into an admission,
  or into a different error, is strictly worse than silence.
- **a refusal with no run is NOT delivered to everybody.** Fanning one pre-warm
  refusal out to every workspace in the estate is how a channel gets muted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import artifact_refusal_notice as arn


class TestTheVocabulary:
    def test_one_trigger_covers_both_reasons(self) -> None:
        """To an operator they are the same event -- something was refused
        admission to what this deployment runs. Two triggers would make a
        complete subscription two boxes to tick instead of one."""
        assert arn.TRIGGER == "artifact:refused"
        assert arn.REASON_VERIFICATION != arn.REASON_ALLOWLIST

    def test_the_trigger_is_one_the_api_accepts(self) -> None:
        """A trigger the notification routes reject is a notification nobody can
        subscribe to -- the feature would exist and be unreachable."""
        from terrapod.services.notification_service import VALID_TRIGGERS

        assert arn.TRIGGER in VALID_TRIGGERS


@pytest.mark.asyncio
class TestNotifyRefusal:
    async def test_a_refusal_during_a_run_reaches_that_runs_workspace(self) -> None:
        db = MagicMock()
        db.get = AsyncMock(return_value=MagicMock(workspace_id="ws-1"))
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enq:
            await arn.notify_refusal(
                db, run_id="run-1", reason=arn.REASON_ALLOWLIST, coordinates="npm/evil/evil.tgz"
            )

        assert enq.await_count == 1
        payload = enq.await_args.args[1]
        assert payload["trigger"] == arn.TRIGGER
        assert payload["workspace_id"] == "ws-1"
        assert payload["reason"] == arn.REASON_ALLOWLIST
        assert payload["coordinates"] == "npm/evil/evil.tgz"

    async def test_a_refusal_with_no_run_is_logged_and_not_delivered(self) -> None:
        """The pre-warm path fetches on nobody's behalf. Inventing a workspace
        -- fanning out to the whole estate -- would turn one refusal into N
        deliveries and teach people to mute the channel."""
        db = MagicMock()
        db.get = AsyncMock()
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enq:
            await arn.notify_refusal(
                db, run_id=None, reason=arn.REASON_VERIFICATION, coordinates="pypi/x/x.whl"
            )

        assert enq.await_count == 0
        assert db.get.await_count == 0, "it must not query for a run it does not have"

    async def test_a_run_that_cannot_be_resolved_is_not_delivered(self) -> None:
        db = MagicMock()
        db.get = AsyncMock(return_value=None)
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enq:
            await arn.notify_refusal(
                db, run_id="gone", reason=arn.REASON_VERIFICATION, coordinates="pypi/x/x.whl"
            )
        assert enq.await_count == 0

    async def test_a_lookup_failure_never_propagates(self) -> None:
        """This runs while handling a refusal. A database error here must not
        replace a clear "refused" with something unrelated."""
        db = MagicMock()
        db.get = AsyncMock(side_effect=RuntimeError("database is away"))
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()):
            await arn.notify_refusal(
                db, run_id="run-1", reason=arn.REASON_ALLOWLIST, coordinates="npm/x/x.tgz"
            )

    async def test_an_enqueue_failure_never_propagates(self) -> None:
        db = MagicMock()
        db.get = AsyncMock(return_value=MagicMock(workspace_id="ws-1"))
        with patch(
            "terrapod.services.scheduler.enqueue_trigger",
            new=AsyncMock(side_effect=RuntimeError("redis is away")),
        ):
            await arn.notify_refusal(
                db, run_id="run-1", reason=arn.REASON_ALLOWLIST, coordinates="npm/x/x.tgz"
            )

    async def test_one_artifact_notifies_once_per_run(self) -> None:
        """A run resolving a hundred dependencies can meet the same refused
        package several times; an operator wants to hear once. The dedup key
        therefore carries the ARTIFACT as well as the run."""
        db = MagicMock()
        db.get = AsyncMock(return_value=MagicMock(workspace_id="ws-1"))
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enq:
            await arn.notify_refusal(
                db, run_id="run-1", reason=arn.REASON_ALLOWLIST, coordinates="npm/evil/evil.tgz"
            )

        key = enq.await_args.kwargs["dedup_key"]
        assert "run-1" in key
        assert "npm/evil/evil.tgz" in key, "two different artifacts must not dedup each other"

    async def test_the_detail_is_bounded(self) -> None:
        """An upstream error body can be large, and this goes into a queued
        payload rather than a log line."""
        db = MagicMock()
        db.get = AsyncMock(return_value=MagicMock(workspace_id="ws-1"))
        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as enq:
            await arn.notify_refusal(
                db,
                run_id="run-1",
                reason=arn.REASON_VERIFICATION,
                coordinates="pypi/x/x.whl",
                detail="y" * 5000,
            )
        assert len(enq.await_args.args[1]["detail"]) <= 500
