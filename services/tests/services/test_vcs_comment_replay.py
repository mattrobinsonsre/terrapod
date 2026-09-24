"""A new PR session does not replay the PR's existing comments (#1796).

Comment polling filters on `last_processed_comment_id`, and a session that has
never processed one has no cursor. Treating that as "everything is new" meant
the moment Terrapod started tracking a PR it ran every `terrapod ...` comment
already on it -- including ones written days earlier, before the App had the
permission or before the workspace was apply_then_merge. The replayed
`terrapod plan` then cancelled the session's own first run.

Paired with #1795 this was a dead end rather than noise: the replayed command
cancelled the run, and the dedup kept a replacement from ever being created.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from terrapod.services.vcs_poller import _comment_is_after_session_start

SESSION_START = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _sess(created_at=SESSION_START):
    return SimpleNamespace(repo="org/repo", pr_number=7, created_at=created_at)


def _comment(created_at: str, cid: str = "1"):
    return SimpleNamespace(id=cid, created_at=created_at, body="terrapod plan")


def test_a_comment_written_before_the_session_is_not_replayed():
    older = (SESSION_START - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    assert _comment_is_after_session_start(_comment(older), _sess()) is False


def test_a_comment_written_after_the_session_is_processed():
    newer = (SESSION_START + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    assert _comment_is_after_session_start(_comment(newer), _sess()) is True


def test_a_comment_written_as_the_session_was_created_is_processed():
    """Inclusive at the boundary: a command that raced session creation was
    still addressed to us, and dropping it is the failure mode this guard is
    supposed to avoid rather than cause."""
    same = SESSION_START.isoformat().replace("+00:00", "Z")
    assert _comment_is_after_session_start(_comment(same), _sess()) is True


def test_the_z_suffix_and_offset_forms_both_parse():
    """GitHub sends `...Z`, GitLab an explicit offset. Neither may be read as
    naive local time -- that would shift the comparison by the host's offset
    and silently replay or drop comments near the boundary."""
    newer_z = (SESSION_START + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    newer_off = (SESSION_START + timedelta(hours=1)).isoformat()
    assert _comment_is_after_session_start(_comment(newer_z), _sess()) is True
    assert _comment_is_after_session_start(_comment(newer_off), _sess()) is True


def test_an_unreadable_timestamp_fails_closed():
    """We cannot tell whether it predates the session, and dispatching a
    command that might be days old is exactly what this guard exists to stop.
    Skipping costs the author a re-comment; running it cancelled their run."""
    assert _comment_is_after_session_start(_comment("not a date"), _sess()) is False
    assert _comment_is_after_session_start(_comment(""), _sess()) is False


def test_a_naive_session_timestamp_is_treated_as_utc():
    """`created_at` is timezone-aware in the model, but a row loaded through a
    driver that drops the tzinfo must not raise on comparison."""
    naive = SESSION_START.replace(tzinfo=None)
    newer = (SESSION_START + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert _comment_is_after_session_start(_comment(newer), _sess(created_at=naive)) is True


# ── the wiring, not just the predicate ───────────────────────────────
#
# The tests above all call the helper directly, and a first mutation check
# showed that is not enough: reverting the CALL SITE to "no cursor means
# everything is new" left every one of them green. A correct helper the
# poller does not consult fixes nothing, so this exercises the path.


async def test_the_poller_does_not_dispatch_a_comment_written_before_the_session():
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from terrapod.services import vcs_poller

    sess = SimpleNamespace(
        id=uuid.uuid4(),
        repo="org/repo",
        pr_number=7,
        created_at=SESSION_START,
        last_processed_comment_id=None,
    )
    old = SimpleNamespace(
        id="100",
        body="terrapod plan",
        author_login="octocat",
        author_user_id="1",
        created_at=(SESSION_START - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        updated_at="",
    )
    new = SimpleNamespace(
        id="200",
        body="terrapod plan",
        author_login="octocat",
        author_user_id="1",
        created_at=(SESSION_START + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        updated_at="",
    )

    sessions = MagicMock()
    sessions.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[sess])))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=sessions)
    conn = SimpleNamespace(id=uuid.uuid4(), provider="github")

    with (
        patch(
            "terrapod.services.github_service.list_pr_comments_typed",
            new_callable=AsyncMock,
            return_value=[old, new],
        ),
        patch("terrapod.services.vcs_poller.enqueue_trigger", new_callable=AsyncMock) as enqueue,
    ):
        await vcs_poller._poll_pr_comments(db, conn, "org/repo")

    dispatched = [c.kwargs.get("dedup_key", "") or c.args[2] for c in enqueue.await_args_list]
    assert enqueue.await_count == 1, dispatched
    # The one that ran must be the comment written AFTER we started tracking.
    assert "200" in str(dispatched[0])
    # And the cursor advances past it, so it is not re-dispatched next cycle.
    assert sess.last_processed_comment_id == "200"
