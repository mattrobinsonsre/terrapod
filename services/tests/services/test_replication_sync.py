"""The follower's pull loop (#960 phase 3, #1110).

The behaviours worth pinning are the ones that fail silently:

- a **stale cursor** must trigger a backfill, not an innocent empty page;
- an **origin-tagged** event must not be applied back onto the node that made
  it, or the pair echoes changes at each other forever;
- a **peer outage** must degrade to "try again later", never to a crash loop on
  the follower.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from terrapod.db.models import ReplicationCursor
from terrapod.services import replication_sync


def _cfg(node_name="node-b", enabled=True):
    return SimpleNamespace(
        role="follower",
        node_name=node_name,
        peer=SimpleNamespace(url="https://peer.example", client_id="peer-a", client_secret="s"),
        replication=SimpleNamespace(
            enabled=enabled, interval_seconds=60, batch_size=500, retention_days=7
        ),
    )


def _resp(status=200, body=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = body or {}
    r.raise_for_status = MagicMock()
    return r


def _db(**kw):
    """An AsyncMock session whose `begin_nested()` behaves like the real
    `AsyncSession`'s — an async context manager, not a coroutine.

    Every row now applies inside a savepoint so one bad row cannot stop the
    stream (#1180). A mock that cannot open one makes every row look unappliable,
    which is a lie about the code under test rather than a finding.
    """
    db = AsyncMock(**kw)
    db.begin_nested = MagicMock(return_value=MagicMock())
    return db


def _db_with_cursor(position="0"):
    db = _db()
    db.scalar.return_value = ReplicationCursor(
        entity_class="*", position=position, backfilling=False
    )
    return db


def _session_ctx(db):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestDisabled:
    @patch("terrapod.services.replication_sync.settings")
    async def test_does_nothing_when_replication_is_off(self, mock_settings):
        """The default for every single-node install — it must not even try to
        reach a peer that was never configured."""
        mock_settings.ha = _cfg(enabled=False)

        with patch("terrapod.services.replication_sync._peer_token") as mock_token:
            await replication_sync.sync_cycle()

        mock_token.assert_not_called()


class TestPeerOutage:
    @patch("terrapod.services.replication_sync.settings")
    async def test_auth_failure_is_not_a_crash(self, mock_settings):
        """A down peer must leave the follower running, not wedge it."""
        mock_settings.ha = _cfg()

        with patch(
            "terrapod.services.replication_sync._peer_token",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("peer down"),
        ):
            await replication_sync.sync_cycle()  # must not raise

    @patch("terrapod.services.replication_sync.settings")
    async def test_a_transient_failure_keeps_the_cached_token(self, mock_settings):
        """Only a REJECTION means the credential is wrong (#1171).

        A timeout, a 5xx or a 429 says nothing about validity, and discarding
        the token there guarantees a fresh mint next cycle — which is how a live
        pair rate-limited its own replication into permanent failure: refused,
        reset, mint, refused again."""
        mock_settings.ha = _cfg()
        token = SimpleNamespace(value="old", expires_at=None)
        replication_sync._token = token

        with patch(
            "terrapod.services.replication_sync._peer_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            await replication_sync.sync_cycle()

        assert replication_sync._token is token

    @patch("terrapod.services.replication_sync.settings")
    async def test_a_rejected_credential_does_drop_the_cached_token(self, mock_settings):
        """The other half: a 401 means this token really is dead, and holding
        it would make every later cycle fail on a stale credential."""
        mock_settings.ha = _cfg()
        replication_sync._token = SimpleNamespace(value="old", expires_at=None)
        request = httpx.Request("POST", "https://peer/oauth/token")
        rejected = httpx.HTTPStatusError(
            "denied", request=request, response=httpx.Response(401, request=request)
        )

        with patch(
            "terrapod.services.replication_sync._peer_token",
            new_callable=AsyncMock,
            side_effect=rejected,
        ):
            await replication_sync.sync_cycle()

        assert replication_sync._token is None


class TestStaleCursor:
    @patch("terrapod.services.replication_sync.backfill_all", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync._peer_token", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_falls_back_to_backfill(
        self, mock_settings, mock_token, mock_request, mock_backfill
    ):
        mock_settings.ha = _cfg()
        mock_token.return_value = "tok"
        mock_request.return_value = _resp(
            body={"data": [], "meta": {"cursor": 900, "stale-cursor": True}}
        )
        db = _db_with_cursor("5")

        with patch("terrapod.db.session.get_db_session", return_value=_session_ctx(db)):
            await replication_sync.sync_cycle()

        mock_backfill.assert_awaited_once()

    @patch("terrapod.services.replication_sync.backfill_all", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync._apply_event", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync._peer_token", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_a_healthy_stream_does_not_backfill(
        self, mock_settings, mock_token, mock_request, mock_apply, mock_backfill
    ):
        mock_settings.ha = _cfg()
        mock_token.return_value = "tok"
        mock_request.return_value = _resp(
            body={
                "data": [
                    {
                        "id": 6,
                        "entity-class": "agent_pools",
                        "entity-id": "x",
                        "op": "upsert",
                        "origin-node": "node-a",
                    }
                ],
                "meta": {"cursor": 6, "stale-cursor": False},
            }
        )
        db = _db_with_cursor("5")

        with patch("terrapod.db.session.get_db_session", return_value=_session_ctx(db)):
            await replication_sync.sync_cycle()

        mock_backfill.assert_not_awaited()
        mock_apply.assert_awaited_once()


class TestOriginTagging:
    @patch("terrapod.services.replication_sync._apply_event", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync._peer_token", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_skips_events_this_node_originated(
        self, mock_settings, mock_token, mock_request, mock_apply
    ):
        """Applying our own change back onto ourselves is how a pair starts
        ping-ponging a row between them."""
        mock_settings.ha = _cfg(node_name="node-b")
        mock_token.return_value = "tok"
        mock_request.return_value = _resp(
            body={
                "data": [
                    {
                        "id": 1,
                        "entity-class": "agent_pools",
                        "entity-id": "a",
                        "op": "upsert",
                        "origin-node": "node-b",
                    },
                    {
                        "id": 2,
                        "entity-class": "agent_pools",
                        "entity-id": "b",
                        "op": "upsert",
                        "origin-node": "node-a",
                    },
                ],
                "meta": {"cursor": 2, "stale-cursor": False},
            }
        )
        db = _db_with_cursor("0")

        with patch("terrapod.db.session.get_db_session", return_value=_session_ctx(db)):
            await replication_sync.sync_cycle()

        assert mock_apply.await_count == 1
        assert mock_apply.await_args[0][3]["entity-id"] == "b"

    @patch("terrapod.services.replication_sync._apply_event", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync._peer_token", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_an_untagged_event_is_still_applied(
        self, mock_settings, mock_token, mock_request, mock_apply
    ):
        """A peer that predates origin tagging must not have every event dropped
        — skipping is only correct when the tag positively matches us."""
        mock_settings.ha = _cfg(node_name="node-b")
        mock_token.return_value = "tok"
        mock_request.return_value = _resp(
            body={
                "data": [
                    {
                        "id": 1,
                        "entity-class": "agent_pools",
                        "entity-id": "a",
                        "op": "upsert",
                        "origin-node": "",
                    }
                ],
                "meta": {"cursor": 1, "stale-cursor": False},
            }
        )
        db = _db_with_cursor("0")

        with patch("terrapod.db.session.get_db_session", return_value=_session_ctx(db)):
            await replication_sync.sync_cycle()

        mock_apply.assert_awaited_once()


class TestApplyEvent:
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_a_deleted_row_is_skipped_not_failed(self, mock_settings, mock_request):
        """404 means the row went away between the event and the read. The later
        delete event settles it; failing here would wedge the whole stream."""
        mock_settings.ha = _cfg()
        mock_request.return_value = _resp(status=404)
        db = _db()

        await replication_sync._apply_event(
            db,
            MagicMock(),
            "tok",
            {"entity-class": "agent_pools", "entity-id": "x", "op": "upsert"},
        )

        db.add.assert_not_called()

    @patch("terrapod.services.replication_sync.settings")
    async def test_an_unknown_class_is_skipped(self, mock_settings):
        """A newer peer replicating a class this node lacks must not wedge the
        stream on one unrecognised row."""
        mock_settings.ha = _cfg()
        db = _db()

        await replication_sync._apply_event(
            db,
            MagicMock(),
            "tok",
            {"entity-class": "invented", "entity-id": "x", "op": "upsert"},
        )

        db.add.assert_not_called()

    @patch("terrapod.services.replication.apply_delete", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_delete_needs_no_entity_read(self, mock_settings, mock_delete):
        mock_settings.ha = _cfg()

        await replication_sync._apply_event(
            AsyncMock(),
            MagicMock(),
            "tok",
            {"entity-class": "agent_pools", "entity-id": "x", "op": "delete"},
        )

        mock_delete.assert_awaited_once()


class TestBackfillPaging:
    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication.apply_upsert", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_walks_pages_until_complete(
        self, mock_settings, mock_request, mock_upsert, mock_reconcile
    ):
        mock_settings.ha = _cfg()
        mock_reconcile.return_value = []
        mock_request.side_effect = [
            _resp(
                body={
                    "data": [{"id": "1", "attributes": {"id": "1"}}],
                    "meta": {"cursor": "1", "complete": False},
                }
            ),
            _resp(
                body={
                    "data": [{"id": "2", "attributes": {"id": "2"}}],
                    "meta": {"cursor": "2", "complete": True},
                }
            ),
        ]
        db = _db_with_cursor("")

        count = await replication_sync.backfill_class(db, MagicMock(), "tok", "agent_pools")

        assert count == 2
        assert mock_request.await_count == 2

    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_persists_progress_per_page(self, mock_settings, mock_request, mock_reconcile):
        """An interrupted backfill must resume, not restart a large class."""
        mock_settings.ha = _cfg()
        mock_reconcile.return_value = []
        mock_request.return_value = _resp(
            body={"data": [], "meta": {"cursor": "abc", "complete": True}}
        )
        db = _db_with_cursor("")

        await replication_sync.backfill_class(db, MagicMock(), "tok", "agent_pools")

        db.commit.assert_awaited()

    @patch("terrapod.services.replication_sync.settings")
    async def test_an_unknown_class_backfills_nothing(self, mock_settings):
        mock_settings.ha = _cfg()

        count = await replication_sync.backfill_class(_db(), MagicMock(), "t", "nope")

        assert count == 0

    @patch("terrapod.services.replication_sync.backfill_class", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_backfill_all_follows_dependency_order(self, mock_settings, mock_class):
        """A join token cannot be inserted before its pool exists."""
        mock_settings.ha = _cfg()

        await replication_sync.backfill_all(_db(), MagicMock(), "tok")

        order = [c.args[3] for c in mock_class.await_args_list]
        assert order.index("agent_pools") < order.index("agent_pool_tokens")


class TestFollowerSafety:
    def test_sync_and_purge_survive_the_periodic_gate(self):
        """A follower runs no scheduled work — except these. Gating the pull
        loop would mean a follower could never converge, which is its only job."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "replication_sync" in _FOLLOWER_SAFE_TASKS
        assert "replication_purge" in _FOLLOWER_SAFE_TASKS


class TestOutboxIsBounded:
    """The outbox is recorded unconditionally, so it must be purged
    unconditionally — otherwise a single-node install with replication off
    accumulates events forever."""

    def test_the_purge_is_registered_without_replication(self):
        import inspect

        from terrapod.api import app as app_module

        source = inspect.getsource(app_module)
        purge = source.index('"replication_purge"')
        gate = source.index("if settings.ha.replication.enabled:")
        assert purge < gate, (
            "replication_purge must be registered before (and outside) the "
            "enabled gate, or an install with replication off never trims its outbox"
        )


class TestBackfillReconciliation:
    """Backfill removes rows the peer no longer has (#1115) — under guards.

    This is the one place replication deletes data that was never deleted
    locally, so the conditions under which it does NOT fire matter at least as
    much as the ones under which it does.
    """

    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication.apply_upsert", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_reconciles_after_a_complete_pass(
        self, mock_settings, mock_request, _upsert, mock_reconcile
    ):
        mock_settings.ha = _cfg()
        mock_reconcile.return_value = []
        mock_request.return_value = _resp(
            body={
                "data": [{"id": "a", "attributes": {"id": "a"}}],
                "meta": {"cursor": "a", "complete": True},
            }
        )

        await replication_sync.backfill_class(
            _db_with_cursor(""), MagicMock(), "tok", "agent_pools"
        )

        mock_reconcile.assert_awaited_once()
        assert mock_reconcile.await_args[0][2] == {"a"}, "must pass the ids it actually saw"

    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication.apply_upsert", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_a_resumed_pass_does_not_reconcile(
        self, mock_settings, mock_request, _upsert, mock_reconcile
    ):
        """A backfill resuming from a cursor never sees the pages it already
        applied, so its view of the peer's rows is partial — acting on it would
        delete everything before the resume point."""
        mock_settings.ha = _cfg()
        mock_request.return_value = _resp(
            body={"data": [], "meta": {"cursor": "z", "complete": True}}
        )

        await replication_sync.backfill_class(
            _db_with_cursor("m"), MagicMock(), "tok", "agent_pools"
        )

        mock_reconcile.assert_not_awaited()

    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_a_failed_pass_does_not_reconcile(
        self, mock_settings, mock_request, mock_reconcile
    ):
        """A truncated set would read as mass deletion."""
        mock_settings.ha = _cfg()
        mock_request.side_effect = httpx.ConnectError("peer went away")

        with pytest.raises(httpx.ConnectError):
            await replication_sync.backfill_class(
                _db_with_cursor(""), MagicMock(), "tok", "agent_pools"
            )

        mock_reconcile.assert_not_awaited()


class TestBackfillIsRepeatable:
    """The class cursor is a resume point, not a high-water mark.

    Leaving it set after a completed pass means the next backfill resumes past
    every row it already saw and re-syncs almost nothing — so a node that ages
    out of the event window a second time would never recover.
    """

    @patch("terrapod.services.replication.reconcile_deletions", new_callable=AsyncMock)
    @patch("terrapod.services.replication.apply_upsert", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_the_cursor_is_cleared_on_completion(
        self, mock_settings, mock_request, _upsert, mock_reconcile
    ):
        mock_settings.ha = _cfg()
        mock_reconcile.return_value = []
        cursor = ReplicationCursor(entity_class="agent_pools", position="", backfilling=False)
        db = _db()
        db.scalar.return_value = cursor
        mock_request.return_value = _resp(
            body={
                "data": [{"id": "a", "attributes": {"id": "a"}}],
                "meta": {"cursor": "a", "complete": True},
            }
        )

        await replication_sync.backfill_class(db, MagicMock(), "tok", "agent_pools")

        assert cursor.position == "", "a completed backfill must not leave a resume point"
        assert cursor.backfilling is False

    @patch("terrapod.services.replication.apply_upsert", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.replication_sync.settings")
    async def test_an_incomplete_pass_keeps_its_resume_point(
        self, mock_settings, mock_request, _upsert
    ):
        mock_settings.ha = _cfg()
        cursor = ReplicationCursor(entity_class="agent_pools", position="", backfilling=False)
        db = _db()
        db.scalar.return_value = cursor
        mock_request.side_effect = [
            _resp(
                body={
                    "data": [{"id": "a", "attributes": {"id": "a"}}],
                    "meta": {"cursor": "a", "complete": False},
                }
            ),
            httpx.ConnectError("peer went away"),
        ]

        with pytest.raises(httpx.ConnectError):
            await replication_sync.backfill_class(db, MagicMock(), "tok", "agent_pools")

        assert cursor.position == "a", "an interrupted backfill must be resumable"
        assert cursor.backfilling is True
