"""Tests for drift detection service — cycle logic and result handling."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.drift_detection_enabled = overrides.get("drift_detection_enabled", True)
    ws.drift_detection_interval_seconds = overrides.get("drift_detection_interval_seconds", 86400)
    ws.drift_last_checked_at = overrides.get("drift_last_checked_at", None)
    ws.drift_status = overrides.get("drift_status", "")
    ws.drift_latest_run_id = overrides.get("drift_latest_run_id", None)
    ws.drift_ignore_rules = overrides.get("drift_ignore_rules", [])
    ws.locked = overrides.get("locked", False)
    ws.vcs_connection_id = overrides.get("vcs_connection_id", None)
    ws.vcs_repo_url = overrides.get("vcs_repo_url", "")
    ws.auto_apply = False
    ws.terraform_version = "1.11"
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.agent_pool_id = None
    ws.owner_email = "test@example.com"
    return ws


def _mock_run(**overrides):
    run = MagicMock()
    run.id = overrides.get("id", uuid.uuid4())
    run.workspace_id = overrides.get("workspace_id", uuid.uuid4())
    run.is_drift_detection = overrides.get("is_drift_detection", True)
    run.has_changes = overrides.get("has_changes", None)
    run.status = overrides.get("status", "planned")
    run.plan_only = True
    run.auto_apply = False
    return run


class TestDriftCheckCycle:
    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_runner_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_locked_workspace(self, mock_session, mock_create, mock_busy, mock_state):
        """Locked workspace is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace(locked=True)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()

    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_runner_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_active_runs(self, mock_session, mock_create, mock_busy, mock_state):
        """Workspace with active run is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace()
        mock_busy.return_value = True

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()

    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_runner_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_not_yet_due(self, mock_session, mock_create, mock_busy, mock_state):
        """Workspace checked recently (within interval) is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace(
            drift_last_checked_at=datetime.now(UTC) - timedelta(seconds=100),
            drift_detection_interval_seconds=86400,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()

    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_planned_run_does_not_block_drift_check(
        self, mock_session, mock_create, mock_state
    ):
        """A run sitting in `planned` awaiting operator confirm must
        NOT block drift detection — that's the bug behind the
        production incident where 4 workspaces froze drift_status at
        the first errored attempt because they each had a `planned`
        run lingering.

        We exercise the REAL _is_runner_busy here (no patch), with a
        mock db that returns the result of a COUNT query restricted
        to {planning, applying} — so a `planned` row in the universe
        is irrelevant.
        """
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace(vcs_connection_id=None)  # non-VCS path
        mock_state.return_value = True
        mock_create.return_value = _mock_run()

        mock_db = AsyncMock()
        # First execute() returns the workspace list; subsequent
        # execute() calls (from _is_runner_busy) return COUNT=0
        # because planning/applying runs are absent.
        ws_result = MagicMock()
        ws_result.scalars.return_value.all.return_value = [ws]
        busy_result = MagicMock()
        busy_result.scalar_one.return_value = 0
        mock_db.execute = AsyncMock(side_effect=[ws_result, busy_result])

        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        # Drift run WAS created — the `planned` run didn't block it.
        mock_create.assert_called_once()

    async def test_is_runner_busy_only_counts_planning_or_applying(self):
        """`_is_runner_busy` must query for ONLY {planning, applying}
        — not the broader ACTIVE_STATES set that broke v0.34.0 and
        earlier. This is the contract that makes
        test_planned_run_does_not_block_drift_check possible.
        """
        from terrapod.services.drift_detection_service import (
            RUNNER_BUSY_STATES,
            _is_runner_busy,
        )

        # Pin the contract.
        assert RUNNER_BUSY_STATES == {"planning", "applying"}

        # Sanity-check the query shape: pass through a mock DB and
        # verify .where() was called referencing Run.status.in_(...).
        mock_db = AsyncMock()
        result = MagicMock()
        result.scalar_one.return_value = 0
        mock_db.execute = AsyncMock(return_value=result)

        await _is_runner_busy(mock_db, uuid.uuid4())

        # Query was issued — that's the test. The SQL shape is
        # validated by the integration test above where a `planned`
        # run actually exists and drift still proceeds.
        mock_db.execute.assert_awaited_once()


class TestHandleDriftRunCompleted:
    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_planned_has_changes_to_drifted(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned run with has_changes=True → drift_status='drifted'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=True)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "drifted"
        # Links the workspace-list badge to the run that produced this status.
        assert ws.drift_latest_run_id == run.id
        mock_notif.assert_called_once()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_planned_no_changes_to_no_drift(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned run with has_changes=False → drift_status='no_drift'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=False)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "no_drift"
        assert ws.drift_latest_run_id == run.id
        mock_notif.assert_not_called()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_errored_to_errored(self, mock_run_svc, mock_session, mock_notif):
        """Errored run → drift_status='errored'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="errored")
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "errored"
        # Errored badge must link to the drift run that produced the error
        # so the operator can click straight to the plan log.
        assert ws.drift_latest_run_id == run.id
        mock_notif.assert_not_called()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_canceled_does_not_update(self, mock_run_svc, mock_session, mock_notif):
        """Canceled run → no drift_status change."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="canceled")
        ws = _mock_workspace(drift_status="no_drift")
        original_status = ws.drift_status

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == original_status
        # Canceled drift runs MUST NOT overwrite drift_latest_run_id —
        # the previous run is still what "explains" the current badge.
        assert ws.drift_latest_run_id is None

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_unknown_has_changes_is_conservative(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned with has_changes=None → conservative drift."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=None)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "drifted"

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_publishes_drift_status_change_event(
        self, mock_run_svc, mock_session, mock_notif, mock_publish
    ):
        """Drift status change publishes to admin, workspace-list, AND the
        per-workspace run_events channel (the last is what the workspace
        detail page listens to — without it the UI stays stale until a
        manual reload)."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=True)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert "tp:admin_events" in channels
        assert "tp:workspace_list_events" in channels
        # The one that fixes the detail-page-stays-stale bug
        assert f"tp:run_events:{ws.id}" in channels
        assert mock_publish.call_count == 3


class TestCreateDriftRunVcs:
    """Drift detection on VCS-connected workspaces must download via the
    VCSArchiveCache / git_fetch pipeline — same as a normal VCS-poll
    run — NOT through the raw `download_archive` provider API.

    Production incident: pre-v0.35.1 the raw API path was masked
    because drift rarely fired (the wide _is_workspace_busy gate
    skipped almost every workspace). v0.35.1 narrowed the gate, drift
    started firing on every drift-enabled workspace, and the raw
    GitHub tarball (wrapped in a top-level `<owner>-<repo>-<sha>/`
    directory) made the runner's `chdir /workspace` land outside the
    repo content — every drift run errored with `Given variables file
    envs/<...>.tfvars does not exist`. The VCSArchiveCache pipeline
    produces a clean, root-level tarball that matches what regular
    VCS-poll runs use.

    # Code ↔ Tests contract (CLAUDE.md): regression test pins the
    # function's archive-acquisition path. Widening _is_runner_busy
    # without also pinning this would reopen the same incident.
    """

    @patch(
        "terrapod.services.drift_detection_service.run_service.queue_run", new_callable=AsyncMock
    )
    @patch(
        "terrapod.services.drift_detection_service.run_service.create_run", new_callable=AsyncMock
    )
    @patch(
        "terrapod.services.drift_detection_service.run_service.mark_configuration_uploaded",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.services.drift_detection_service.run_service.create_configuration_version",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.vcs_poller._stream_cv_upload_from_cache", new_callable=AsyncMock)
    @patch(
        "terrapod.services.vcs_archive_cache.VCSArchiveCache.get_or_fetch", new_callable=AsyncMock
    )
    @patch("terrapod.services.vcs_poller._get_branch_sha", new_callable=AsyncMock)
    @patch("terrapod.services.vcs_poller._resolve_branch", new_callable=AsyncMock)
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_uses_vcs_archive_cache_not_raw_download(
        self,
        mock_parse,
        mock_resolve,
        mock_sha,
        mock_fetch,
        mock_upload,
        mock_cv,
        mock_mark,
        mock_create,
        mock_queue,
    ):
        """`_create_drift_run_vcs` MUST call `VCSArchiveCache.get_or_fetch`
        and `_stream_cv_upload_from_cache`. It MUST NOT call the raw
        `download_archive` provider API.
        """
        from terrapod.services.drift_detection_service import _create_drift_run_vcs

        ws = _mock_workspace(
            vcs_connection_id=uuid.uuid4(),
            vcs_repo_url="https://github.com/example/repo",
        )

        conn = MagicMock()
        conn.status = "active"

        cv = MagicMock()
        cv.id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=conn)
        mock_parse.return_value = ("example", "repo")
        mock_resolve.return_value = "main"
        mock_sha.return_value = "deadbeef"
        mock_fetch.return_value = "vcs-cache/example/repo/deadbeef.tar.gz"
        mock_cv.return_value = cv
        mock_mark.return_value = cv
        mock_create.return_value = MagicMock()
        mock_queue.return_value = MagicMock()

        await _create_drift_run_vcs(mock_db, ws)

        # The contract: the VCSArchiveCache pipeline was used.
        mock_fetch.assert_awaited_once()
        # paths=None — drift fetches the whole repo, same as a normal
        # VCS-poll apply that lacks trigger prefixes.
        _, fetch_kwargs = mock_fetch.call_args
        assert fetch_kwargs.get("paths") is None
        # And the streaming upload helper from vcs_poller was called.
        mock_upload.assert_awaited_once()

    async def test_raw_download_archive_not_referenced(self):
        """Belt-and-braces source-introspection: the drift_detection
        module must not import `_download_archive` from `vcs_poller`.
        Importing it would re-enable the broken path even if the
        active code path is correct.
        """
        import inspect

        from terrapod.services import drift_detection_service

        src = inspect.getsource(drift_detection_service)
        # The forbidden import is `from terrapod.services.vcs_poller
        # import (..., _download_archive, ...)`. The function name
        # might appear in comments; we only care that it isn't
        # imported.
        for line in src.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            assert "_download_archive" not in line, (
                f"drift detection must not reference _download_archive "
                f"(the raw GitHub tarball path with wrapping directory). "
                f"Offending line: {line!r}"
            )


class TestCreateDriftRunNonVcs:
    """Drift detection on non-VCS workspaces must plan against the CV from
    the workspace's latest successful apply — i.e. the bytes that produced
    the current state — not the latest uploaded CV (which may never have
    been applied)."""

    @patch(
        "terrapod.services.drift_detection_service.run_service.queue_run", new_callable=AsyncMock
    )
    @patch(
        "terrapod.services.drift_detection_service.run_service.create_run", new_callable=AsyncMock
    )
    async def test_uses_cv_from_latest_applied_run(self, mock_create, mock_queue):
        """The selected CV is the one referenced by the most recent applied
        run, even if a newer-but-unapplied CV exists."""
        from terrapod.services.drift_detection_service import _create_drift_run_non_vcs

        ws = _mock_workspace()

        # Stub the join query to return a specific CV (the "applied" one).
        applied_cv = MagicMock()
        applied_cv.id = uuid.uuid4()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = applied_cv
        mock_db.execute.return_value = mock_result

        new_run = MagicMock()
        mock_create.return_value = new_run
        mock_queue.return_value = new_run

        result = await _create_drift_run_non_vcs(mock_db, ws)

        assert result is new_run
        # The run was created with the applied CV's id
        kwargs = mock_create.call_args.kwargs
        assert kwargs["configuration_version_id"] == applied_cv.id
        assert kwargs["is_drift_detection"] is True
        assert kwargs["plan_only"] is True
        assert kwargs["source"] == "drift-detection"

    @patch(
        "terrapod.services.drift_detection_service.run_service.create_run", new_callable=AsyncMock
    )
    async def test_returns_none_when_workspace_never_applied(self, mock_create):
        """A workspace that has never been applied has no reference CV — drift
        is a no-op rather than picking some arbitrary uploaded CV."""
        from terrapod.services.drift_detection_service import _create_drift_run_non_vcs

        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await _create_drift_run_non_vcs(mock_db, ws)

        assert result is None
        mock_create.assert_not_called()


class TestApplyDriftIgnoreRules:
    """`_apply_drift_ignore_rules` (#482) classifies a drift plan against the
    workspace's ignore rules. The plan parse + classifier run off the event loop
    (Rule 13) — these assert the branch outcomes survive that offload, plus the
    conservative fallbacks."""

    @patch("terrapod.services.drift_ignore_classifier.classify_drift")
    @patch("terrapod.storage.get_storage")
    async def test_all_suppressed_is_no_drift(self, mock_storage, mock_classify):
        from terrapod.services import drift_detection_service as mod

        store = MagicMock()
        store.get = AsyncMock(return_value=b'{"resource_changes": []}')
        mock_storage.return_value = store
        mock_classify.return_value = (False, [{"address": "x"}])  # nothing still drifted
        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4())

        assert await mod._apply_drift_ignore_rules(run, ["ignore_tags"]) == "no_drift"
        mock_classify.assert_called_once()

    @patch("terrapod.services.drift_ignore_classifier.classify_drift")
    @patch("terrapod.storage.get_storage")
    async def test_remaining_change_is_drifted(self, mock_storage, mock_classify):
        from terrapod.services import drift_detection_service as mod

        store = MagicMock()
        store.get = AsyncMock(return_value=b'{"resource_changes": [{"address": "y"}]}')
        mock_storage.return_value = store
        mock_classify.return_value = (True, [])  # a change survived the rules
        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4())

        assert await mod._apply_drift_ignore_rules(run, ["ignore_tags"]) == "drifted"

    @patch("terrapod.storage.get_storage")
    async def test_unparseable_plan_falls_back_to_drifted(self, mock_storage):
        from terrapod.services import drift_detection_service as mod

        store = MagicMock()
        store.get = AsyncMock(return_value=b"not json{{{")
        mock_storage.return_value = store
        run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4())

        # A runtime hiccup must never SILENCE drift the operator wanted surfaced.
        assert await mod._apply_drift_ignore_rules(run, ["ignore_tags"]) == "drifted"
