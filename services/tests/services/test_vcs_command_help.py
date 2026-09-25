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


def test_the_routable_verbs_are_the_verbs_the_dispatcher_actually_routes():
    """The test above pins the help against a HAND-WRITTEN list, so it can only
    catch a verb someone remembered to add to that list -- which is not the
    failure it describes. The dispatcher routes with a chain of
    `cmd.verb == "..."` and never reads `_ROUTABLE_VERBS`, so a verb added to
    the chain alone is routed, undocumented, and green.

    Enumerate from the source instead, the way the wire-completeness gate does,
    so the list cannot drift from the routing it claims to describe.
    """
    import inspect
    import re

    routed = set(re.findall(r'cmd\.verb\s*==\s*"([a-z-]+)"', inspect.getsource(disp)))
    assert routed, "found no `cmd.verb == ...` comparisons; the routing shape changed"
    assert routed == set(disp._ROUTABLE_VERBS), (
        "_ROUTABLE_VERBS and the dispatcher disagree about what is routable. "
        f"routed only: {sorted(routed - set(disp._ROUTABLE_VERBS))}; "
        f"listed only: {sorted(set(disp._ROUTABLE_VERBS) - routed)}"
    )


class TestProseNeverReachesAnyReplyPath:
    """#1836 added the prose guard INSIDE `_route`, i.e. after the session
    lookup — so it never covered the no-session branch #1799 had added one
    commit earlier.

    With the GitHub App installed org-wide (the common deployment) that branch
    fires on every repo with no Terrapod workspace at all. A passing mention on
    an unrelated PR drew an eyes reaction, a six-line explanation about
    apply-then-merge workspaces, and a thumbs-down: three API calls and a
    comment on a repo that has nothing to do with Terrapod.
    """

    @staticmethod
    def _payload(body: str) -> dict:
        return {
            "body": body,
            "connection_id": str(uuid.uuid4()),
            "repo": "org/unrelated-service",
            "pr_number": 7,
            "comment_id": "c1",
            "actor_login": "someone",
        }

    async def _dispatch(self, body: str):
        """Returns (reacted, commented) — what the PR would have seen."""
        conn = SimpleNamespace(id=uuid.uuid4(), provider="github")
        db = AsyncMock()
        db.get = AsyncMock(return_value=conn)
        db.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)  # no session
        )

        class _Ctx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *a):
                return False

        with (
            patch.object(disp, "get_db_session", return_value=_Ctx()),
            patch.object(disp, "_react", new=AsyncMock(return_value="eyes")) as react,
            patch.object(disp, "_unreact", new=AsyncMock()),
            patch.object(disp, "_post_comment", new=AsyncMock()) as comment,
        ):
            await disp.handle_vcs_comment_dispatch(self._payload(body))
        return react.await_count, comment.await_count

    async def test_a_passing_mention_on_an_unrelated_pr_is_silent(self):
        reacted, commented = await self._dispatch("terrapod is working well now")
        assert commented == 0, "Terrapod commented on a PR that mentioned it in passing"
        assert reacted == 0, "Terrapod reacted to prose"

    async def test_prose_in_a_longer_sentence_is_silent(self):
        reacted, commented = await self._dispatch("terrapod has been really solid this week")
        assert (reacted, commented) == (0, 0)

    async def test_a_real_command_still_gets_its_no_session_explanation(self):
        """The #1799 behaviour must survive: a genuine command on a PR with no
        session is told why nothing happened, rather than ignored."""
        reacted, commented = await self._dispatch("terrapod apply")
        assert commented == 1, "a real command lost its 'received and ignored' reply"
        assert reacted >= 1

    async def test_a_typo_still_gets_its_explanation(self):
        """A mistyped verb followed by a FLAG is a command attempt, not prose."""
        reacted, commented = await self._dispatch("terrapod aply -W web")
        assert commented == 1
