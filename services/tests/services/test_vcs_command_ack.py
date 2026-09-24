"""A `terrapod ...` comment is acknowledged, and a dropped one says why (#1799).

Before this, the only signal that a command had been received was whatever it
went on to do — which for the drop paths was nothing at all. The author could
not tell "Terrapod never saw it" from "Terrapod saw it and had nothing to do",
and those want opposite responses: retype it, or go and fix the workspace.

These drive `handle_vcs_comment_dispatch` itself rather than the helpers. The
helpers are easy to test and prove little: #1796 shipped a correct helper the
poller never consulted, and every test passed. The reaction has to happen
BEFORE the session lookup or the drop paths — the ones that most need
acknowledging — are exactly the ones that never get it.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import vcs_command_dispatcher as disp

CONN_ID = uuid.uuid4()


def _conn(provider="github"):
    return SimpleNamespace(id=CONN_ID, provider=provider)


def _sess(state="open"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        vcs_connection_id=CONN_ID,
        repo="org/repo",
        pr_number=7,
        state=state,
        head_sha="deadbeef",
    )


def _ws(name="prod"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        vcs_repo_url="https://github.com/org/repo",
        vcs_workflow="apply_then_merge",
    )


def _db(conn, sess, workspaces):
    """A db whose first execute() is the session lookup, second the workspaces."""
    sess_res = MagicMock()
    sess_res.scalar_one_or_none = MagicMock(return_value=sess)
    ws_res = MagicMock()
    ws_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=workspaces)))

    db = AsyncMock()
    db.get = AsyncMock(return_value=conn)
    db.execute = AsyncMock(side_effect=[sess_res, ws_res])
    return db


def _payload(body="terrapod plan", comment_id="4242"):
    return {
        "connection_id": str(CONN_ID),
        "repo": "org/repo",
        "pr_number": 7,
        "comment_id": comment_id,
        "actor_login": "octocat",
        "actor_user_id": "1",
        "body": body,
    }


@asynccontextmanager
async def _session_cm(db):
    yield db


def _run(db, payload, *, route_accepted=True):
    """Drive the handler with the provider calls mocked out.

    Returns (react_mock, unreact_mock, post_mock) so a test can assert on
    what reached the provider.
    """
    return patch.multiple(
        disp,
        get_db_session=MagicMock(return_value=_session_cm(db)),
        _react=AsyncMock(return_value=99),
        _unreact=AsyncMock(),
        _post_comment=AsyncMock(),
        _post_reply=AsyncMock(),
        _route=AsyncMock(return_value=route_accepted),
    )


async def test_a_command_is_acknowledged_the_moment_it_arrives():
    db = _db(_conn(), _sess(), [_ws()])
    with _run(db, _payload()):
        await disp.handle_vcs_comment_dispatch(_payload())
        first = disp._react.await_args_list[0]

    # The eyes go on before anything can decide to drop it.
    assert first.args[4] == disp.ACK_RECEIVED
    assert first.args[3] == "4242"


async def test_a_routed_command_ends_with_a_thumbs_up_and_no_eyes():
    db = _db(_conn(), _sess(), [_ws()])
    with _run(db, _payload()):
        await disp.handle_vcs_comment_dispatch(_payload())
        contents = [c.args[4] for c in disp._react.await_args_list]
        unreacted = disp._unreact.await_count

    assert contents == [disp.ACK_RECEIVED, disp.ACK_DONE]
    # The eyes are removed, so the final state reads as one verdict rather
    # than an ambiguous pair.
    assert unreacted == 1


async def test_a_command_the_router_refused_ends_with_a_thumbs_down():
    db = _db(_conn(), _sess(), [_ws()])
    with _run(db, _payload(), route_accepted=False):
        await disp.handle_vcs_comment_dispatch(_payload())
        contents = [c.args[4] for c in disp._react.await_args_list]

    assert contents == [disp.ACK_RECEIVED, disp.ACK_REJECTED]


async def test_a_pr_with_no_session_is_told_so_rather_than_ignored():
    """The case the issue is really about: a command on a PR Terrapod is not
    tracking used to be indistinguishable from one it never received."""
    db = _db(_conn(), None, [])
    with _run(db, _payload()):
        await disp.handle_vcs_comment_dispatch(_payload())
        posted = disp._post_comment.await_args
        calls = disp._post_comment.await_count
        contents = [c.args[4] for c in disp._react.await_args_list]

    assert calls == 1
    assert "not tracking this" in posted.args[3]
    assert contents == [disp.ACK_RECEIVED, disp.ACK_REJECTED]


async def test_a_closed_session_is_treated_as_no_session():
    db = _db(_conn(), _sess(state="closed"), [])
    with _run(db, _payload()):
        await disp.handle_vcs_comment_dispatch(_payload())
        assert disp._post_comment.await_count == 1


async def test_a_comment_that_is_not_a_command_is_left_entirely_alone():
    """The webhook enqueues EVERY PR comment with no pre-filter, so reacting
    before the parse would put eyes on ordinary conversation."""
    db = _db(_conn(), _sess(), [_ws()])
    with _run(db, _payload(body="looks good to me, merging")):
        await disp.handle_vcs_comment_dispatch(_payload(body="looks good to me, merging"))
        reacted = disp._react.await_count
        posted = disp._post_comment.await_count

    assert (reacted, posted) == (0, 0)


async def test_a_payload_with_no_comment_id_still_dispatches():
    """Reacting is an extra, not a precondition — a payload from an older
    producer must not lose its command over a missing acknowledgement."""
    db = _db(_conn(), _sess(), [_ws()])
    with _run(db, _payload(comment_id="")):
        await disp.handle_vcs_comment_dispatch(_payload(comment_id=""))
        assert disp._react.await_count == 0
        disp._route.assert_awaited_once()


async def test_a_provider_that_refuses_the_reaction_does_not_lose_the_command():
    """An App installation that never accepted the permission gets no emoji
    and everything else works. `_react` swallows and returns None, so the
    settle path must cope with having no reaction id to remove."""
    db = _db(_conn(), _sess(), [_ws()])
    with patch.multiple(
        disp,
        get_db_session=MagicMock(return_value=_session_cm(db)),
        _react=AsyncMock(return_value=None),
        _unreact=AsyncMock(),
        _post_comment=AsyncMock(),
        _post_reply=AsyncMock(),
        _route=AsyncMock(return_value=True),
    ):
        await disp.handle_vcs_comment_dispatch(_payload())
        disp._route.assert_awaited_once()
        # Nothing to remove, so no removal is attempted.
        assert disp._unreact.await_count == 0


# ── the reason replies, at the routing layer ──────────────────────────


async def test_an_unknown_verb_names_the_token_it_did_not_understand():
    sess = _sess()
    db = AsyncMock()
    with patch.object(disp, "_post_reply", new_callable=AsyncMock) as reply:
        accepted = await disp._route(
            db, disp.parse("terrapod plna"), _conn(), sess, [_ws()], "octocat", "1"
        )

    body = reply.await_args.args[2]
    assert "`plna`" in body
    # Still carries the usage table — naming the typo replaces the bare
    # table, it does not replace the help.
    assert "terrapod plan" in body
    assert accepted is False


async def test_an_explicit_help_request_is_answered_and_counts_as_handled():
    db = AsyncMock()
    with patch.object(disp, "_post_reply", new_callable=AsyncMock) as reply:
        accepted = await disp._route(
            db, disp.parse("terrapod help"), _conn(), _sess(), [_ws()], "octocat", "1"
        )

    assert reply.await_args.args[2] == disp._HELP_BODY
    assert accepted is True


async def test_prose_opening_with_the_prefix_gets_no_reply_at_all():
    """`terrapod is broken` parses as an unknown verb, but "is" is not a typo
    of anything — quoting it would read as the parser talking nonsense.

    This used to answer with the generic help table instead, which fixed the
    nonsense and left the noise: a twelve-line usage table on someone's PR
    because their sentence happened to start with the product's name, seen by
    every reviewer, with no way to switch it off (#1836).

    Silence serves the original concern better than the table did. A comment
    that was not addressed to Terrapod gets no answer from Terrapod.
    """
    db = AsyncMock()
    with patch.object(disp, "_post_reply", new_callable=AsyncMock) as reply:
        await disp._route(
            db, disp.parse("terrapod is broken"), _conn(), _sess(), [_ws()], "octocat", "1"
        )

    reply.assert_not_awaited()


async def test_a_named_workspace_that_is_not_affected_says_which_name():
    db = AsyncMock()
    with patch.object(disp, "_post_reply", new_callable=AsyncMock) as reply:
        accepted = await disp._route(
            db, disp.parse("terrapod apply -W missing"), _conn(), _sess(), [], "octocat", "1"
        )

    assert "`missing`" in reply.await_args.args[2]
    assert accepted is False


async def test_no_affected_workspaces_at_all_explains_that_instead():
    db = AsyncMock()
    with patch.object(disp, "_post_reply", new_callable=AsyncMock) as reply:
        accepted = await disp._route(
            db, disp.parse("terrapod apply"), _conn(), _sess(), [], "octocat", "1"
        )

    assert reply.await_args.args[2] == disp._NO_CANDIDATES_BODY
    assert accepted is False


def test_every_reason_says_the_command_was_received():
    """Each body has one job before any advice: distinguish "received and
    dropped" from "never arrived"."""
    bodies = [
        disp._NO_SESSION_BODY,
        disp._NO_CANDIDATES_BODY,
        disp._no_workspace_body("x"),
    ]
    for b in bodies:
        assert "received this command" in b, b
