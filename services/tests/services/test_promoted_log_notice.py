"""A promoted log says that it is a tail and not the whole run (#2526).

The reconciler already promotes the live Redis snapshot to storage when a run
goes terminal with no stored log, which is what keeps a run from having no log
at all five minutes later -- the Redis key is written with a 300-second TTL.
What it did not do is say what the promoted copy is: the live stream holds the
last few hundred lines, so a 109 KB configure log arrived in storage as 45 KB
that reads exactly like a complete one.

A line in the object rather than an attribute on the run, deliberately: it
reaches the viewer, the download and an operator curling the artifact without
an attribute to add to the API, the SDK, the provider, the MCP tools and the UI.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import run_reconciler
from terrapod.services.run_reconciler import LOG_TAIL_NOTICE, _persist_live_log_if_missing
from terrapod.storage.protocol import ObjectNotFoundError


def _run():
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    return run


class _Storage:
    def __init__(self, *, already_stored: bool):
        self.already_stored = already_stored
        self.put_calls: list[tuple[str, bytes]] = []

    async def get(self, key):
        if self.already_stored:
            return b"the runner's own upload"
        raise ObjectNotFoundError(key)

    async def put(self, key, data, **_kw):
        self.put_calls.append((key, data))


def _patches(storage, live):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=live)
    return (
        patch("terrapod.storage.get_storage", return_value=storage),
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
    )


class TestThePromotedCopyAnnouncesItself:
    async def test_the_notice_leads_and_the_tail_follows(self):
        storage = _Storage(already_stored=False)
        a, b = _patches(storage, b"TASK [one]\nPLAY RECAP\n")
        with a, b:
            await _persist_live_log_if_missing(_run(), "configure")
        assert len(storage.put_calls) == 1
        _key, body = storage.put_calls[0]
        assert body.startswith(LOG_TAIL_NOTICE)
        assert body.endswith(b"TASK [one]\nPLAY RECAP\n")

    async def test_the_notice_says_what_it_is_and_where_to_look(self):
        """A truncated log that does not say so is worse than a short one: the
        operator reads the PLAY RECAP, sees no failure above it, and concludes
        the run failed for no reason."""
        text = LOG_TAIL_NOTICE.decode()
        assert "not the whole run" in text
        assert "maxTokenTTLSeconds" in text

    async def test_a_string_snapshot_is_handled_too(self):
        """A Redis client configured to decode responses hands back `str`."""
        storage = _Storage(already_stored=False)
        a, b = _patches(storage, "TASK [one]\n")
        with a, b:
            await _persist_live_log_if_missing(_run(), "configure")
        assert storage.put_calls[0][1] == LOG_TAIL_NOTICE + b"TASK [one]\n"


class TestItDoesNotTouchACompleteLog:
    async def test_a_stored_log_is_left_alone(self):
        """The runner's own upload is the whole log, so prepending a notice to
        it would be a false claim on every successful run."""
        storage = _Storage(already_stored=True)
        a, b = _patches(storage, b"TASK [one]\n")
        with a, b:
            await _persist_live_log_if_missing(_run(), "configure")
        assert storage.put_calls == []

    async def test_an_empty_snapshot_stores_nothing(self):
        """A notice with no log under it is a stored object that says only that
        there is no log, which the absence of an object already says."""
        storage = _Storage(already_stored=False)
        a, b = _patches(storage, None)
        with a, b:
            await _persist_live_log_if_missing(_run(), "configure")
        assert storage.put_calls == []


class TestItIsStillBestEffort:
    async def test_a_storage_failure_does_not_raise(self):
        """This runs on the terminal-transition path for every run; a storage
        problem must not leave the run untransitioned."""

        class _Broken(_Storage):
            async def put(self, key, data, **_kw):
                raise RuntimeError("boom")

        a, b = _patches(_Broken(already_stored=False), b"x")
        with a, b:
            await _persist_live_log_if_missing(_run(), "configure")  # must not raise


class TestTheCallSitesStillPromote:
    """The notice is only reachable through them, and a configure's own
    promotion goes to the configure key -- a two-way phase choice once sent it
    to the apply key, which on a composite overwrites the apply's log."""

    def test_both_terminal_handlers_promote(self):
        import ast
        import inspect

        for fn in (run_reconciler._handle_succeeded, run_reconciler._handle_failed):
            tree = ast.parse(inspect.getsource(fn))
            called = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            assert "_persist_live_log_if_missing" in called, fn.__name__

    async def test_a_configure_promotes_to_the_configure_key(self):
        from terrapod.storage.keys import configure_log_key

        run = _run()
        storage = _Storage(already_stored=False)
        a, b = _patches(storage, b"x")
        with a, b:
            await _persist_live_log_if_missing(run, "configure")
        assert storage.put_calls[0][0] == configure_log_key(str(run.workspace_id), str(run.id))
