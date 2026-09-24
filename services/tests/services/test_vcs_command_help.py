"""`terrapod help` posts the command list (#1797).

It used to audit-log and return, pending a "phase 6" that never arrived — so
the command documented in docs/vcs-workflows.md did nothing visible. Worse,
the parser maps every UNKNOWN verb to `help`, so a typo was silent too: the
author could not tell a mistyped command from one Terrapod never received.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from terrapod.services import vcs_command_dispatcher as disp


def _sess(provider="github"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        vcs_connection_id=uuid.uuid4(),
        repo="org/repo",
        pr_number=7,
    )


def _db(conn):
    db = AsyncMock()
    db.get = AsyncMock(return_value=conn)
    return db


async def test_help_posts_the_command_list_on_github():
    conn = SimpleNamespace(id=uuid.uuid4(), provider="github")
    with patch(
        "terrapod.services.github_service.create_pr_comment", new_callable=AsyncMock
    ) as post:
        await disp._post_reply(_db(conn), _sess(), disp._HELP_BODY)

    post.assert_awaited_once()
    body = post.await_args.args[4]
    for verb in ("terrapod plan", "terrapod apply", "terrapod unlock", "terrapod merge"):
        assert verb in body
    # owner/repo are split from the session, not parsed out of a URL.
    assert post.await_args.args[1] == "org"
    assert post.await_args.args[2] == "repo"


async def test_help_posts_on_gitlab_too():
    conn = SimpleNamespace(id=uuid.uuid4(), provider="gitlab")
    with patch(
        "terrapod.services.gitlab_service.create_mr_comment", new_callable=AsyncMock
    ) as post:
        await disp._post_reply(_db(conn), _sess(), disp._HELP_BODY)
    post.assert_awaited_once()


async def test_a_reply_that_cannot_be_posted_does_not_raise():
    """Best-effort: the command this accompanies has already run, so failing
    the dispatch over the reply would turn a cosmetic problem into a real one."""
    conn = SimpleNamespace(id=uuid.uuid4(), provider="github")
    with patch(
        "terrapod.services.github_service.create_pr_comment",
        new_callable=AsyncMock,
        side_effect=RuntimeError("403"),
    ):
        await disp._post_reply(_db(conn), _sess(), "hi")  # must not raise


async def test_a_session_with_no_connection_is_skipped():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    with patch(
        "terrapod.services.github_service.create_pr_comment", new_callable=AsyncMock
    ) as post:
        await disp._post_reply(db, _sess(), "hi")
    post.assert_not_awaited()


def test_the_help_text_lists_every_routable_verb():
    """If a verb is added to the dispatcher and not to this table, the command
    exists but nobody is told about it."""
    for verb in disp._ROUTABLE_VERBS:
        assert f"terrapod {verb}" in disp._HELP_BODY, verb
