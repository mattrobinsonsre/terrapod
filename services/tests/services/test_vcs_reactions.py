"""Reacting to a command comment, on both providers (#1799).

These pin the request each provider actually receives. The two APIs are not
the same shape — GitHub addresses a PR comment as an *issue* comment and needs
no PR number, GitLab addresses a note through its merge request and does — and
a wrong URL fails in the least visible way available: the reaction is
best-effort, so a 404 is swallowed and the command still works. Nobody would
notice for a long time.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import github_service, gitlab_service


def _conn(provider="github"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        server_url="",
        token="t",
        github_app_id="1",
        github_installation_id="2",
    )


def _resp(payload=None):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload if payload is not None else {"id": 55}
    r.raise_for_status = MagicMock()
    r.headers = {}
    return r


class TestGitHubReactions:
    async def test_adding_a_reaction_posts_to_the_issue_comment_endpoint(self):
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(
                github_service, "_github_request", new=AsyncMock(return_value=_resp())
            ) as req,
        ):
            got = await github_service.add_comment_reaction(_conn(), "org", "repo", 4242, "eyes")

        method, url = req.await_args.args[0], req.await_args.args[1]
        assert method == "POST"
        # A PR comment is an ISSUE comment on GitHub — `/pulls/` here 404s.
        assert url.endswith("/repos/org/repo/issues/comments/4242/reactions")
        assert req.await_args.kwargs["json"] == {"content": "eyes"}
        # The id comes back so the eyes can be removed once the verdict lands.
        assert got == 55

    async def test_removing_a_reaction_deletes_by_reaction_id(self):
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(
                github_service, "_github_request", new=AsyncMock(return_value=_resp())
            ) as req,
        ):
            await github_service.remove_comment_reaction(_conn(), "org", "repo", 4242, 55)

        assert req.await_args.args[0] == "DELETE"
        assert req.await_args.args[1].endswith("/issues/comments/4242/reactions/55")


class TestGitLabAwardEmoji:
    async def test_awarding_goes_through_the_merge_request_note(self):
        captured = {}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                captured["url"] = url
                captured["json"] = kw.get("json")
                return _resp({"id": 77})

        with (
            patch.object(gitlab_service.httpx, "AsyncClient", return_value=_Client()),
            patch.object(gitlab_service.vcs_rate_limit, "record", new=AsyncMock()) as rec,
        ):
            got = await gitlab_service.add_comment_reaction(
                _conn("gitlab"), "org", "repo", 7, 4242, "eyes"
            )

        # GitLab routes the note through its merge request, unlike GitHub.
        assert captured["url"].endswith("/merge_requests/7/notes/4242/award_emoji")
        assert captured["json"] == {"name": "eyes"}
        assert got == 77
        # A raw-client write still has to be counted against the rate limit;
        # only the retry is deliberately skipped on mutations.
        rec.assert_awaited_once()

    async def test_removing_an_award_counts_against_the_rate_limit_too(self):
        captured = {}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def delete(self, url, **kw):
                captured["url"] = url
                return _resp()

        with (
            patch.object(gitlab_service.httpx, "AsyncClient", return_value=_Client()),
            patch.object(gitlab_service.vcs_rate_limit, "record", new=AsyncMock()) as rec,
        ):
            await gitlab_service.remove_comment_reaction(
                _conn("gitlab"), "org", "repo", 7, 4242, 77
            )

        assert captured["url"].endswith("/merge_requests/7/notes/4242/award_emoji/77")
        rec.assert_awaited_once()


class TestTheTwoAPIsDoNotShareASpelling:
    """#2350. The thumbs never appeared on GitHub for several releases.

    This class replaces a test called
    `test_the_emoji_names_are_spelled_the_way_both_apis_want_them`, which
    asserted `":" not in name` and `name.islower()` — properties `thumbsup`
    and `+1` both have. So it passed on the defect while its NAME claimed to
    check what the APIs accept, which is why the gap survived review: the
    weaker question looked like the stronger one.

    The honest question is membership in the set GitHub documents, so that is
    what these assert.
    """

    # GitHub's documented `content` enum, transcribed from
    # https://docs.github.com/en/rest/reactions/reactions rather than from the
    # implementation -- a copy of our own mapping would agree with itself.
    GITHUB_ACCEPTS = {"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"}

    def test_every_acknowledgement_reaches_github_as_a_name_it_accepts(self):
        from terrapod.services import vcs_command_dispatcher as disp

        for name in (disp.ACK_RECEIVED, disp.ACK_DONE, disp.ACK_REJECTED):
            sent = github_service.github_reaction_content(name)
            assert sent in self.GITHUB_ACCEPTS, f"GitHub would 422 {name!r} (sent as {sent!r})"

    def test_the_thumbs_are_translated_and_the_eyes_are_not(self):
        assert github_service.github_reaction_content("thumbsup") == "+1"
        assert github_service.github_reaction_content("thumbsdown") == "-1"
        assert github_service.github_reaction_content("eyes") == "eyes"

    async def test_the_translation_happens_on_the_wire_not_just_in_a_helper(self):
        """Driven through the request, because a helper nothing calls is the
        shape of defect this file now carries two of."""
        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(
                github_service, "_github_request", new=AsyncMock(return_value=_resp())
            ) as req,
        ):
            from terrapod.services import vcs_command_dispatcher as disp

            await github_service.add_comment_reaction(_conn(), "org", "repo", 4242, disp.ACK_DONE)

        assert req.await_args.kwargs["json"] == {"content": "+1"}

    async def test_gitlab_still_receives_the_bare_name(self):
        """The other direction. GitLab's award-emoji API takes `thumbsup` and
        does NOT take `+1`, so translating centrally would have moved the bug
        rather than fixed it."""
        captured = {}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured["json"] = json
                return _resp()

        with (
            patch.object(gitlab_service.httpx, "AsyncClient", _Client),
            patch.object(gitlab_service.vcs_rate_limit, "record", new=AsyncMock()),
        ):
            from terrapod.services import vcs_command_dispatcher as disp

            await gitlab_service.add_comment_reaction(
                _conn("gitlab"), "org", "repo", 7, 4242, disp.ACK_DONE
            )

        assert captured["json"] == {"name": "thumbsup"}

    async def test_a_name_github_cannot_take_is_refused_rather_than_posted(self):
        """Refusing beats posting-and-hoping, because `_react` swallows the
        error either way: a 422 is invisible, and that invisibility is the
        whole reason this went unnoticed."""
        import pytest

        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(
                github_service, "_github_request", new=AsyncMock(return_value=_resp())
            ) as req,
        ):
            with pytest.raises(ValueError, match="does not accept"):
                await github_service.add_comment_reaction(
                    _conn(), "org", "repo", 4242, "partyparrot"
                )

        req.assert_not_awaited()


class TestTheDispatcherLayerItself:
    """`_react`'s body was executed by no test in the repository.

    That matters more after #2350 than before it: `github_reaction_content`
    now RAISES on a name GitHub cannot take, where the old code posted and let
    GitHub answer 422. The only thing between that raise and a lost
    `terrapod ...` command is `_react`'s `except Exception` — so narrowing that
    handler would turn a cosmetic missing emoji into a dropped apply, and
    nothing would have failed.

    The provider branch is the other untested half: inverting it sends GitHub
    connections to GitLab's function (wrong arity, swallowed `TypeError`) and
    no acknowledgement appears on either provider. And translating inside
    `_react` for both branches would simply move #2350 to GitLab — which the
    direct-call tests above cannot see, because they never go through here.
    """

    def _conn_obj(self, provider):
        return SimpleNamespace(
            id=uuid.uuid4(),
            provider=provider,
            server_url="",
            token="t",
            github_app_id="1",
            github_installation_id="2",
        )

    async def test_github_goes_through_githubs_function_with_the_translated_name(self):
        from terrapod.services import vcs_command_dispatcher as disp

        with patch.object(
            github_service, "add_comment_reaction", new=AsyncMock(return_value=99)
        ) as gh:
            got = await disp._react(self._conn_obj("github"), "org/repo", 7, "4242", disp.ACK_DONE)

        assert got == 99
        # Provider-neutral name in; GitHub's own function does the translating.
        assert gh.await_args.args[-1] == disp.ACK_DONE

    async def test_gitlab_goes_through_gitlabs_function_and_keeps_the_bare_name(self):
        from terrapod.services import vcs_command_dispatcher as disp

        with patch.object(
            gitlab_service, "add_comment_reaction", new=AsyncMock(return_value=77)
        ) as gl:
            got = await disp._react(self._conn_obj("gitlab"), "org/repo", 7, "4242", disp.ACK_DONE)

        assert got == 77
        assert gl.await_args.args[-1] == "thumbsup", (
            "translating here would move #2350 to GitLab, which takes no `+1`"
        )

    async def test_an_unmappable_name_is_swallowed_rather_than_losing_the_command(self):
        """The property the raise depends on. `_react` is called before the
        command is routed, so an escaping exception drops the command."""
        from terrapod.services import vcs_command_dispatcher as disp

        with (
            patch.object(github_service, "get_installation_token", new=AsyncMock(return_value="t")),
            patch.object(github_service, "_github_request", new=AsyncMock(return_value=_resp())),
        ):
            got = await disp._react(self._conn_obj("github"), "org/repo", 7, "4242", "partyparrot")

        assert got is None, "the ValueError escaped _react and would lose the command"


def test_every_acknowledgement_constant_is_covered_not_just_the_three_we_listed():
    """Derived, with a floor — so a FOURTH `ACK_*` cannot recur #2350.

    The class above iterates a literal tuple of three names. That is a
    fixed-arity check wearing the shape of a completeness check: adding
    `ACK_QUEUED = "hourglass"` (a valid GitLab award name, absent from
    GitHub's set) and using it would 422 on GitHub, be swallowed, and fail
    nothing.
    """
    from terrapod.services import vcs_command_dispatcher as disp

    names = {v for k, v in vars(disp).items() if k.startswith("ACK_") and isinstance(v, str)}
    assert len(names) >= 3, f"the matcher stopped finding the constants: {names}"
    for name in sorted(names):
        sent = github_service.github_reaction_content(name)
        assert sent in github_service.GITHUB_REACTION_CONTENTS, f"{name!r} -> {sent!r}"


def test_our_accepted_set_is_githubs_documented_set():
    """The production frozenset was never compared to the independent
    transcription, so shrinking it would refuse a legitimate future reaction
    while every other test passed."""
    assert github_service.GITHUB_REACTION_CONTENTS == {
        "+1",
        "-1",
        "laugh",
        "confused",
        "heart",
        "hooray",
        "rocket",
        "eyes",
    }
