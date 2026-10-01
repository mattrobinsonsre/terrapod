"""Being able to comment on a pull request is not authorization to act on it.

Two things came out of auditing the comment surface.

The first is that `terrapod merge` existed at all. It was undocumented in the
command table everyone reads, was handled BEFORE the candidate check so it did
not need a workspace to be affected, and merged the pull request with the
GitHub App's own credentials. Anyone who could type a comment could merge.
It is removed, not gated -- a merge that should happen despite an incomplete
apply is made on the provider, by someone the repository trusts to merge.

The second is that the remaining commands -- `plan`, `apply`, `unlock` -- were
authorized by the ability to comment and nothing else. On a public repository
that is everyone. `terrapod apply` applies real infrastructure changes, and
`terrapod unlock` releases a workspace lock that branch protection has no
opinion about whatsoever. They now require push access to the repository,
asked of the provider at command time.

These drive `handle_vcs_comment_dispatch` rather than the helpers, for the
reason the sibling ack tests give: a correct helper nothing consults is the
failure mode this surface has actually shipped before.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    sess_res = MagicMock()
    sess_res.scalar_one_or_none = MagicMock(return_value=sess)
    ws_res = MagicMock()
    ws_res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=workspaces)))
    db = AsyncMock()
    db.get = AsyncMock(return_value=conn)
    db.execute = AsyncMock(side_effect=[sess_res, ws_res])
    return db


def _payload(body="terrapod apply", login="octocat", user_id="4242"):
    return {
        "connection_id": str(CONN_ID),
        "repo": "org/repo",
        "pr_number": 7,
        "comment_id": "99",
        "actor_login": login,
        "actor_user_id": user_id,
        "body": body,
    }


@asynccontextmanager
async def _session_cm(db):
    yield db


def _run(db):
    """Drive the handler with everything below the gate mocked out."""
    return patch.multiple(
        disp,
        get_db_session=MagicMock(return_value=_session_cm(db)),
        _react=AsyncMock(return_value=99),
        _unreact=AsyncMock(),
        _post_comment=AsyncMock(),
        _post_reply=AsyncMock(),
        _route=AsyncMock(return_value=True),
    )


# ── The gate, end to end through the dispatcher ──────────────────────


class TestTheGateDecidesWhetherACommandRuns:
    async def test_a_commenter_who_can_push_is_routed(self):
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=True)) as gate,
        ):
            await disp.handle_vcs_comment_dispatch(_payload())
            routed = disp._route.await_count
            posted = disp._post_comment.await_count

        gate.assert_awaited_once()
        assert routed == 1
        assert posted == 0

    async def test_a_commenter_who_cannot_push_is_refused_and_told_why(self):
        """Refused, and NOT silently. Silence cannot be told apart from a
        command that was never received, which is the complaint the whole
        acknowledgement surface exists to answer."""
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=False)),
        ):
            await disp.handle_vcs_comment_dispatch(_payload())
            routed = disp._route.await_count
            body = disp._post_comment.await_args.args[3]
            reactions = [c.args[4] for c in disp._react.await_args_list]

        assert routed == 0, "a commenter without push access had their command routed"
        assert body == disp._NO_PUSH_ACCESS_BODY
        assert "push to this repository" in body
        assert reactions == [disp.ACK_RECEIVED, disp.ACK_REJECTED]

    async def test_a_lookup_that_could_not_answer_refuses_rather_than_assuming_yes(self):
        """Fails CLOSED. A rate limit or a provider outage must not read as a
        grant -- that would make the gate removable by anyone able to make the
        provider slow."""
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=None)),
        ):
            await disp.handle_vcs_comment_dispatch(_payload())
            routed = disp._route.await_count
            body = disp._post_comment.await_args.args[3]

        assert routed == 0
        # A different message from the refusal: "you cannot" and "we could not
        # tell" want opposite responses from the author -- ask someone else, or
        # try again.
        assert body == disp._UNKNOWN_ACCESS_BODY
        assert body != disp._NO_PUSH_ACCESS_BODY

    @pytest.mark.parametrize("verb", ["plan", "apply", "unlock"])
    async def test_every_acting_verb_is_gated(self, verb):
        """Every verb that does something. `help` is the one exemption, below."""
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=False)),
        ):
            await disp.handle_vcs_comment_dispatch(_payload(body=f"terrapod {verb}"))
            routed = disp._route.await_count

        assert routed == 0, f"`terrapod {verb}` ran for someone who cannot push"

    async def test_help_is_exempt_and_costs_no_lookup(self):
        """The gate exists because commenting is a low bar to take an ACTION, and
        `help` takes none — it lists what the public docs list. Gating it would
        cost the thing that matters most: an unrecognised verb resolves to
        `help`, so a contributor who mistypes would get a permissions lecture
        instead of the usage table.

        Asserting the lookup is never made, not merely that the command ran:
        checking and then ignoring the answer would still spend a provider call
        on every passing typo.
        """
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=False)) as gate,
        ):
            await disp.handle_vcs_comment_dispatch(_payload(body="terrapod help"))
            routed = disp._route.await_count

        assert routed == 1, "`terrapod help` was refused"
        gate.assert_not_awaited()

    async def test_an_unrecognised_verb_still_gets_the_usage_table(self):
        """The consequence of the exemption, and the reason for it: a mistyped
        verb resolves to `help`, so it must not draw a refusal."""
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=False)) as gate,
        ):
            await disp.handle_vcs_comment_dispatch(_payload(body="terrapod aply"))

        gate.assert_not_awaited()

    async def test_prose_never_reaches_the_gate(self):
        """A passing mention is not a command, so it must not draw a
        permissions lecture onto an unrelated pull request -- the noise the
        prose guard was added to stop."""
        db = _db(_conn(), _sess(), [_ws()])
        with (
            _run(db),
            patch.object(disp, "_actor_push_access", AsyncMock(return_value=False)) as gate,
        ):
            await disp.handle_vcs_comment_dispatch(_payload(body="terrapod is working well now"))
            posted = disp._post_comment.await_count

        gate.assert_not_awaited()
        assert posted == 0


# ── `_actor_push_access`: provider dispatch + the switch ─────────────


class TestTheGateAsksTheRightProvider:
    async def test_github_is_asked_about_the_login(self):
        with patch(
            "terrapod.services.github_service.actor_has_push_access",
            new_callable=AsyncMock,
        ) as gh:
            gh.return_value = True
            assert await disp._actor_push_access(_conn("github"), "org/repo", "octocat", "42")
        gh.assert_awaited_once_with(gh.await_args.args[0], "org", "repo", "octocat")

    async def test_gitlab_is_asked_about_the_numeric_user_id(self):
        """GitLab's members API is keyed on the id, not the username -- and the
        id survives a rename, where a username would follow whoever took the
        handle."""
        with patch(
            "terrapod.services.gitlab_service.actor_has_push_access",
            new_callable=AsyncMock,
        ) as gl:
            gl.return_value = True
            assert await disp._actor_push_access(_conn("gitlab"), "grp/proj", "octocat", "42")
        gl.assert_awaited_once_with(gl.await_args.args[0], "grp", "proj", "42")

    async def test_an_unknown_provider_cannot_be_asked_so_it_is_refused(self):
        assert await disp._actor_push_access(_conn("svn"), "org/repo", "octocat", "42") is None

    async def test_a_malformed_repo_is_refused(self):
        assert await disp._actor_push_access(_conn(), "no-slash", "octocat", "42") is None

    async def test_the_switch_off_restores_the_old_behaviour_without_a_lookup(self):
        """The escape hatch for an operator who relied on the previous model.
        It must not merely ignore the ANSWER -- it must not ask, or a provider
        outage would start refusing commands on a deployment that opted out."""
        with (
            patch("terrapod.config.settings.vcs.require_push_permission_for_commands", False),
            patch(
                "terrapod.services.github_service.actor_has_push_access",
                new_callable=AsyncMock,
            ) as gh,
        ):
            assert await disp._actor_push_access(_conn(), "org/repo", "octocat", "42") is True
        gh.assert_not_awaited()

    def test_the_switch_is_on_by_default(self):
        from terrapod.config import VCSConfig

        assert VCSConfig().require_push_permission_for_commands is True


# ── The removed command stays removed ────────────────────────────────


class TestTerrapodMergeIsGone:
    def test_the_verb_is_not_recognised(self):
        from terrapod.services.vcs_command_parser import _KNOWN_VERBS

        assert "merge" not in _KNOWN_VERBS
        assert "merge" not in disp._ROUTABLE_VERBS

    def test_the_dispatcher_routes_no_merge_verb(self):
        """Source-introspection, because the routing is a chain of
        `cmd.verb == "..."` that no list is consulted for: a branch added back
        to the chain alone would be routed, undocumented, and green."""
        import inspect
        import re

        routed = set(re.findall(r'cmd\.verb\s*==\s*"([a-z-]+)"', inspect.getsource(disp)))
        assert routed, "found no `cmd.verb == ...` comparisons; the routing shape changed"
        assert "merge" not in routed

    def test_nothing_offers_a_comment_driven_merge(self):
        """The dispatcher has no path to a provider merge, and the auto-merge
        module no longer exports one to it."""
        import inspect

        from terrapod.services import vcs_auto_merge

        assert not hasattr(vcs_auto_merge, "force_merge")
        # Matched on the import and the call, not on the module name: the
        # dispatcher NAMES `vcs_auto_merge` in a comment explaining that
        # workspace-configured auto-merge is a different thing and survives,
        # and a bare substring check would read that explanation as the
        # offence it warns against.
        src = inspect.getsource(disp)
        assert "from terrapod.services.vcs_auto_merge import" not in src
        assert "force_merge(" not in src
        assert "merge_pull_request" not in src

    def test_workspace_configured_auto_merge_is_untouched(self):
        """The feature that SHOULD merge is a different one, and this is the
        line between them: it fires from a completed apply, only for a
        workspace whose own `auto_merge` setting asked for it, and only once
        every affected workspace has met its gate. Removing the comment command
        must not have taken it with it."""
        import inspect

        from terrapod.services import vcs_auto_merge

        assert callable(vcs_auto_merge.handle_vcs_apply_completed)
        assert callable(vcs_auto_merge._execute_merge)
        src = inspect.getsource(vcs_auto_merge)
        assert "w.auto_merge for w in affected" in src
        assert "_meets_required_state" in src
