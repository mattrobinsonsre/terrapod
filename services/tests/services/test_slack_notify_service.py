"""Tests for Slack run notifications (#556): opt-in gate, trigger filter, shapes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import slack_notify_service as sn


def _run(**kw):
    base = {
        "id": "run-1",
        "workspace_id": "ws-1",
        "resource_additions": 1,
        "resource_changes": 2,
        "resource_destructions": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_db_no_ai():
    """db.execute(...).scalar_one_or_none() → None (no AI summary)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    return db


@pytest.mark.asyncio
async def test_enqueue_filters_non_slack_triggers():
    enq = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enq):
        await sn.enqueue_slack_notify(_run(), "run:planning")
        await sn.enqueue_slack_notify(_run(), "run:planned")
        await sn.enqueue_slack_notify(_run(), "run:created")
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_noop_when_slack_disabled():
    """Slack off (the default) → nothing enqueued, so no orphan trigger lands
    with no handler (which would log-spam every run)."""
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", False),
        patch("terrapod.services.scheduler.enqueue_trigger", enq),
    ):
        for t in ("run:needs_attention", "run:completed", "run:errored", "run:drift_detected"):
            await sn.enqueue_slack_notify(_run(), t)
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_passes_the_four_slack_triggers():
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch("terrapod.services.scheduler.enqueue_trigger", enq),
    ):
        for t in ("run:needs_attention", "run:completed", "run:errored", "run:drift_detected"):
            await sn.enqueue_slack_notify(_run(), t)
    assert enq.await_count == 4
    # dedup key is per (run, trigger)
    keys = {c.kwargs["dedup_key"] for c in enq.await_args_list}
    assert keys == {
        f"slacknotif:run-1:{t}"
        for t in ("run:needs_attention", "run:completed", "run:errored", "run:drift_detected")
    }


@pytest.mark.asyncio
async def test_ai_enabled_defers_needs_attention_and_errored():
    """With AI on, the fresh-AI events are deferred to the summariser; the
    immediate events (completed/drift) still enqueue."""
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.ai_summary, "enabled", True),
        patch("terrapod.services.scheduler.enqueue_trigger", enq),
    ):
        await sn.enqueue_slack_notify(_run(), "run:needs_attention")
        await sn.enqueue_slack_notify(_run(), "run:errored")
        await sn.enqueue_slack_notify(_run(), "run:completed")
        await sn.enqueue_slack_notify(_run(), "run:drift_detected")
    assert enq.await_count == 2  # only completed + drift_detected
    triggers = {c.args[1]["trigger"] for c in enq.await_args_list}
    assert triggers == {"run:completed", "run:drift_detected"}


@pytest.mark.asyncio
async def test_summariser_bypass_re_fires_deferred_event():
    """The summariser re-fires the deferred event via _from_summariser=True."""
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.ai_summary, "enabled", True),
        patch("terrapod.services.scheduler.enqueue_trigger", enq),
    ):
        await sn.enqueue_slack_notify(_run(), "run:needs_attention", _from_summariser=True)
    enq.assert_awaited_once()
    assert enq.await_args.args[1]["trigger"] == "run:needs_attention"


@pytest.mark.asyncio
async def test_ai_disabled_enqueues_everything_immediately():
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.ai_summary, "enabled", False),
        patch("terrapod.services.scheduler.enqueue_trigger", enq),
    ):
        for t in ("run:needs_attention", "run:errored", "run:completed", "run:drift_detected"):
            await sn.enqueue_slack_notify(_run(), t)
    assert enq.await_count == 4


@pytest.mark.asyncio
async def test_footer_shows_deployment_label_when_set():
    """Multi-deployment (#691): a per-deployment label renders in the message
    footer so a shared channel can attribute each Terrapod; absent when unset."""
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    with (
        patch.object(settings, "external_url", "https://terrapod.example.com"),
        patch.object(settings.slack, "label", "us-west-2"),
    ):
        msg = await sn._build_message(db, run, ws, "run:completed")
    assert "Terrapod: *us-west-2*" in str(msg["blocks"])

    with (
        patch.object(settings, "external_url", "https://terrapod.example.com"),
        patch.object(settings.slack, "label", ""),
    ):
        msg2 = await sn._build_message(db, run, ws, "run:completed")
    assert "Terrapod: *" not in str(msg2["blocks"])


@pytest.mark.asyncio
async def test_needs_attention_message_has_approve_discard_buttons():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    with patch.object(settings, "external_url", "https://terrapod.example.com"):
        msg = await sn._build_message(db, run, ws, "run:needs_attention")
    action_blocks = [b for b in msg["blocks"] if b.get("type") == "actions"]
    assert action_blocks, "needs_attention must carry an actions block"
    action_ids = {e["action_id"] for e in action_blocks[0]["elements"]}
    assert action_ids == {sn.ACTION_APPROVE, sn.ACTION_DISCARD}
    # the run values are the run id (what the interaction handler reads)
    assert all(e["value"] == "run-1" for e in action_blocks[0]["elements"])


@pytest.mark.asyncio
async def test_terminal_messages_have_no_buttons():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    for trigger in ("run:completed", "run:errored", "run:drift_detected"):
        with patch.object(settings, "external_url", "https://terrapod.example.com"):
            msg = await sn._build_message(db, run, ws, trigger)
        assert not [b for b in msg["blocks"] if b.get("type") == "actions"]


@pytest.mark.asyncio
async def test_deep_link_omitted_when_external_url_unset():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    with patch.object(settings, "external_url", ""):
        msg = await sn._build_message(db, run, ws, "run:completed")
    # no "Open in Terrapod" context element when external_url is unset
    ctxs = [b for b in msg["blocks"] if b.get("type") == "context"]
    joined = str(ctxs)
    assert "Open in Terrapod" not in joined


@pytest.mark.asyncio
async def test_deep_link_uses_external_url_only():
    ws = SimpleNamespace(id="ws-1", name="prod")
    run = _run()
    db = _fake_db_no_ai()
    with patch.object(settings, "external_url", "https://users.example.com"):
        msg = await sn._build_message(db, run, ws, "run:completed")
    assert "https://users.example.com/workspaces/ws-1/runs/run-1" in str(msg["blocks"])


@pytest.mark.asyncio
async def test_handler_noop_when_workspace_not_opted_in():
    """Opt-in: a workspace with no slack_channel posts nothing."""
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod", slack_channel="")

    class CM:
        async def __aenter__(self):
            return SimpleNamespace(get=AsyncMock(side_effect=[run, ws]))

        async def __aexit__(self, *a):
            return False

    bot = MagicMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.slack, "bot_token", "xoxb-x"),
        patch("terrapod.db.session.get_db_session", return_value=CM()),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    bot.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_handler_noop_when_slack_disabled():
    bot = MagicMock()
    with (
        patch.object(settings.slack, "enabled", False),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    bot.chat_postMessage.assert_not_called()


def _db_cm(run, ws):
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # no AI summary

    class CM:
        async def __aenter__(self):
            return SimpleNamespace(
                get=AsyncMock(side_effect=[run, ws]), execute=AsyncMock(return_value=result)
            )

        async def __aexit__(self, *a):
            return False

    return CM()


@pytest.mark.asyncio
async def test_needs_attention_posts_parent_stores_ref_and_threads_plan():
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod", slack_channel="#deploys")
    bot = MagicMock()
    bot.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "C1", "ts": "111.2"})
    redis = SimpleNamespace(
        hset=AsyncMock(), expire=AsyncMock(), hgetall=AsyncMock(return_value={})
    )
    upload = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.slack, "bot_token", "xoxb-x"),
        patch("terrapod.db.session.get_db_session", return_value=_db_cm(run, ws)),
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
        patch("terrapod.services.slack_notify_service._upload_plan_file", upload),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    # parent posted (top-level, not threaded), msgref stored, plan threaded under it
    assert bot.chat_postMessage.await_args.kwargs.get("thread_ts") is None
    redis.hset.assert_awaited_once()
    assert redis.hset.await_args.kwargs["mapping"] == {"channel": "C1", "ts": "111.2"}
    upload.assert_awaited_once()
    assert upload.await_args.kwargs.get("thread_ts") == "111.2"


@pytest.mark.asyncio
async def test_needs_attention_idempotent_when_ref_already_exists():
    """Idempotency (#687): if a parent approval message already exists (posted by
    the summariser or a prior backfill), a second fire must NOT post a duplicate."""
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod", slack_channel="#deploys")
    bot = MagicMock()
    bot.chat_postMessage = AsyncMock()
    # ref already present → already posted
    redis = SimpleNamespace(hgetall=AsyncMock(return_value={"channel": "C1", "ts": "111.2"}))
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.slack, "bot_token", "xoxb-x"),
        patch("terrapod.db.session.get_db_session", return_value=_db_cm(run, ws)),
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    bot.chat_postMessage.assert_not_called()


def _backfill_db(rows):
    result = MagicMock()
    result.all.return_value = rows

    class CM:
        async def __aenter__(self):
            return SimpleNamespace(execute=AsyncMock(return_value=result))

        async def __aexit__(self, *a):
            return False

    return CM()


@pytest.mark.asyncio
async def test_backfill_noop_when_ai_disabled():
    """Nothing is deferred unless AI is on, so the backfill short-circuits."""
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.ai_summary, "enabled", False),
        patch("terrapod.services.slack_notify_service.enqueue_slack_notify", enq),
    ):
        await sn.slack_approval_backfill_cycle()
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_posts_overdue_unposted_and_skips_already_posted():
    """Overdue planned-approval runs with no parent get re-fired (bypassing the
    AI defer); ones that already have a parent are skipped."""
    rows = [("run-unposted", "ws-1"), ("run-posted", "ws-2")]

    async def _hgetall(key):
        return {"channel": "C", "ts": "1"} if "run-posted" in key else {}

    redis = SimpleNamespace(hgetall=AsyncMock(side_effect=_hgetall))
    enq = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.ai_summary, "enabled", True),
        patch("terrapod.db.session.get_db_session", return_value=_backfill_db(rows)),
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch("terrapod.services.slack_notify_service.enqueue_slack_notify", enq),
    ):
        await sn.slack_approval_backfill_cycle()
    # only the unposted run is re-fired, as a summariser-bypass needs_attention
    enq.assert_awaited_once()
    posted_run, trigger = enq.await_args.args[0], enq.await_args.args[1]
    assert str(posted_run.id) == "run-unposted"
    assert trigger == "run:needs_attention"
    assert enq.await_args.kwargs.get("_from_summariser") is True


@pytest.mark.asyncio
async def test_completed_threads_under_the_approval_parent():
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod", slack_channel="#deploys")
    bot = MagicMock()
    bot.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "C1", "ts": "999.9"})
    # parent approval message exists in the ref
    redis = SimpleNamespace(hgetall=AsyncMock(return_value={"channel": "C1", "ts": "111.2"}))
    upload = AsyncMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.slack, "bot_token", "xoxb-x"),
        patch("terrapod.db.session.get_db_session", return_value=_db_cm(run, ws)),
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
        patch("terrapod.services.slack_notify_service._upload_plan_file", upload),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:completed"}
        )
    # result threads under the parent (the follow-up ping), no standalone plan upload
    bot.chat_postMessage.assert_awaited_once()
    assert bot.chat_postMessage.await_args.kwargs.get("thread_ts") == "111.2"
    upload.assert_not_awaited()


class TestThePlanLogUploadedToSlackCarriesNoSignedURL:
    """GHSA-5x47-chx9-ww6c.

    The plan log is uploaded byte for byte, and a Slack channel is routinely a
    wider audience than the workspace's access list — Slack then retains the
    file, outside any Terrapod authorization decision. OpenTofu redacts values
    it knows are sensitive in its DISPLAY output, so a well-behaved plan log is
    not the credential dump the plan JSON would be; what it still carries is a
    signed storage URL on any download or upload failure path, and that is a
    live credential anyone in the channel can replay.
    """

    def test_a_signed_storage_url_loses_its_credential(self):
        from terrapod.services.slack_notify_service import _strip_url_queries

        line = b"error fetching https://tp.example/storage/get/plans/x?expires=99&sig=DEADBEEF: 500"
        out = _strip_url_queries(line)
        assert b"sig=" not in out
        assert b"DEADBEEF" not in out
        assert b"expires=" not in out
        # The URL is still recognisable, so the log stays useful for debugging.
        assert b"https://tp.example/storage/get/plans/x" in out

    def test_an_ordinary_url_is_left_alone(self):
        from terrapod.services.slack_notify_service import _strip_url_queries

        line = b"see https://tp.example/workspaces/ws-1/runs/run-2 for detail"
        assert _strip_url_queries(line) == line

    def test_several_urls_on_one_line_are_all_redacted(self):
        from terrapod.services.slack_notify_service import _strip_url_queries

        line = b"a https://x/1?sig=AAA b https://y/2?sig=BBB c"
        out = _strip_url_queries(line)
        assert b"AAA" not in out and b"BBB" not in out

    def test_a_userinfo_credential_is_stripped(self):
        """A `tofu init` line for a private module source prints this verbatim,
        and it is a longer-lived credential than the signed URL."""
        from terrapod.services.slack_notify_service import _strip_url_queries

        out = _strip_url_queries(b"clone https://x-access-token:ghp_SECRET@github.com/o/r.git")
        assert b"ghp_SECRET" not in out
        assert b"github.com/o/r.git" in out

    def test_an_at_sign_in_a_path_is_not_mistaken_for_userinfo(self):
        from terrapod.services.slack_notify_service import _strip_url_queries

        line = b"see https://tp.example/modules/a/b@v1.2.3/main.tf"
        assert _strip_url_queries(line) == line

    def test_an_uppercase_scheme_is_still_redacted(self):
        from terrapod.services.slack_notify_service import _strip_url_queries

        assert b"SIG" not in _strip_url_queries(b"HTTPS://tp.example/k?sig=SIG")

    def test_redaction_is_linear_not_quadratic(self):
        """The original pattern was O(n^2) and ran synchronously inside an async
        coroutine: 218 KB of `http://` took 44 seconds and blocked the worker's
        event loop. Plan logs are attacker-influenced and routinely tens of MB."""
        import time

        from terrapod.services.slack_notify_service import _strip_url_queries

        payload = b"http://" * 128_000  # ~875 KB, no "?" anywhere
        started = time.perf_counter()
        _strip_url_queries(payload)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"redaction took {elapsed:.1f}s on 875 KB — backtracking is back"

    async def test_a_url_split_across_a_read_boundary_is_still_redacted(self):
        """The subtle one. A URL cannot span a newline but easily spans a
        64 KiB read, and a regex applied to raw chunks would miss exactly the
        ones that straddle a boundary."""
        import os
        import tempfile
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services import slack_notify_service as svc

        full = (
            b"line one\nfetch https://tp.example/storage/get/k?expires=1&sig=SPLITSECRET failed\n"
        )
        # Split BEFORE the "?", which is the case that actually distinguishes
        # the fix from the bug. Splitting mid-signature does NOT: a naive
        # per-chunk regex still matches the URL in chunk 1 and rewrites it to
        # "...?(redacted)", so the assertion on the whole token passes anyway.
        # Cutting before the "?" leaves chunk 2 with no scheme, so a per-chunk
        # regex never matches it and writes the signature verbatim.
        cut = full.index(b"?")

        async def _stream(_key):
            yield full[:cut]
            yield full[cut:]

        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        storage.get_stream = _stream

        written = {}
        real_mkstemp = tempfile.mkstemp

        def _capture_mkstemp(**kwargs):
            fd, path = real_mkstemp(**kwargs)
            written["path"] = path
            return fd, path

        client = MagicMock()
        client.files_upload_v2 = AsyncMock()

        with (
            patch.object(svc, "get_storage", return_value=storage, create=True),
            patch("terrapod.storage.get_storage", return_value=storage),
            patch("tempfile.mkstemp", side_effect=_capture_mkstemp),
            patch.object(svc, "_resolve_tmpdir", return_value=None),
        ):
            uploaded = {}

            async def _grab(**kwargs):
                with open(kwargs["file"], "rb") as fh:
                    uploaded["body"] = fh.read()

            client.files_upload_v2 = AsyncMock(side_effect=_grab)
            await svc._upload_plan_file(client, "#c", "ws-1", "run-1")

        body = uploaded.get("body", b"")
        assert b"SPLITSECRET" not in body, body
        assert b"line one" in body
        assert "path" in written, "mkstemp was never called — the test proved nothing"
        assert not os.path.exists(written["path"]), "temp file was not cleaned up"
