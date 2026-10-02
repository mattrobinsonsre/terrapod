"""Tests for git-auth source resolution (#1028).

The server resolves each git-auth variable's *source* into a concrete credential
before delivery, so the runner phase is source-agnostic. Static values pass
through; a ``vcs_connection`` source mints a short-lived token from the referenced
VCS connection. A cred that can't be resolved is dropped (never fails the run).

The exception is the GitLab switch below: a GitLab connection's token cannot be
narrowed, so delivering it is a refusal that FAILS the run rather than a drop.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import git_auth_service


@dataclass
class _RV:  # stand-in for ResolvedVariable (only the fields the service reads)
    key: str
    value: str
    category: str


def _var(category, key, **cred):
    return _RV(key=key, value=json.dumps(cred), category=category)


async def test_static_http_passes_through_with_defaults():
    resolved = [
        _var("git_http_auth", "github.com/org", source="static", token="ghp_X", rewrite="to_https")
    ]
    out = await git_auth_service.resolve_git_auth(AsyncMock(), resolved)
    assert len(out) == 1
    v = json.loads(out[0]["value"])
    assert out[0]["category"] == "git_http_auth" and out[0]["key"] == "github.com/org"
    assert v == {"username": "x-access-token", "token": "ghp_X", "rewrite": "to_https"}


async def test_static_http_without_token_is_dropped():
    resolved = [_var("git_http_auth", "github.com", source="static", rewrite="none")]
    assert await git_auth_service.resolve_git_auth(AsyncMock(), resolved) == []


async def test_ssh_passes_through_verbatim():
    resolved = [
        _var("git_ssh_auth", "gitlab.com", private_key="KEY", known_hosts="KH", rewrite="to_ssh")
    ]
    out = await git_auth_service.resolve_git_auth(AsyncMock(), resolved)
    assert out[0]["category"] == "git_ssh_auth"
    assert json.loads(out[0]["value"])["private_key"] == "KEY"


async def test_non_git_categories_ignored():
    resolved = [_RV("k", "v", "terraform"), _RV("k2", "v2", "env")]
    assert await git_auth_service.resolve_git_auth(AsyncMock(), resolved) == []


async def test_malformed_value_is_skipped():
    resolved = [_RV(key="github.com", value="not-json", category="git_http_auth")]
    assert await git_auth_service.resolve_git_auth(AsyncMock(), resolved) == []


# --- vcs_connection source (flagship) ---------------------------------------


async def _db_returning(conn):
    db = AsyncMock()
    db.get = AsyncMock(return_value=conn)
    return db


async def test_github_connection_mints_installation_token():
    conn = MagicMock(provider="github")
    db = await _db_returning(conn)
    resolved = [
        _var(
            "git_http_auth",
            "github.com/org",
            source="vcs_connection",
            vcs_connection_id=f"vcs-{uuid.uuid4()}",
            rewrite="to_https",
        )
    ]
    with patch.object(
        git_auth_service.github_service,
        "get_installation_token",
        new=AsyncMock(return_value="ghs_MINTED"),
    ):
        out = await git_auth_service.resolve_git_auth(db, resolved)
    v = json.loads(out[0]["value"])
    assert v == {"username": "x-access-token", "token": "ghs_MINTED", "rewrite": "to_https"}


async def test_gitlab_connection_uses_stored_token_as_oauth2():
    """The behaviour an operator gets once they have accepted the trade."""
    conn = MagicMock(provider="gitlab", token="glpat_STORED")
    db = await _db_returning(conn)
    resolved = [_gitlab_var()]
    with patch.object(settings.vcs.gitlab, "allow_token_delivery_to_runners", True):
        out = await git_auth_service.resolve_git_auth(db, resolved)
    v = json.loads(out[0]["value"])
    assert v == {"username": "oauth2", "token": "glpat_STORED", "rewrite": "none"}


async def test_unknown_connection_is_dropped():
    db = await _db_returning(None)  # db.get returns None
    resolved = [
        _var(
            "git_http_auth",
            "github.com",
            source="vcs_connection",
            vcs_connection_id=f"vcs-{uuid.uuid4()}",
            rewrite="none",
        )
    ]
    assert await git_auth_service.resolve_git_auth(db, resolved) == []


async def test_missing_connection_id_is_dropped():
    resolved = [_var("git_http_auth", "github.com", source="vcs_connection", rewrite="none")]
    assert await git_auth_service.resolve_git_auth(AsyncMock(), resolved) == []


async def test_invalid_connection_id_is_dropped():
    resolved = [
        _var(
            "git_http_auth",
            "github.com",
            source="vcs_connection",
            vcs_connection_id="not-a-uuid",
            rewrite="none",
        )
    ]
    assert await git_auth_service.resolve_git_auth(AsyncMock(), resolved) == []


async def test_mint_failure_drops_entry_never_raises():
    conn = MagicMock(provider="github")
    db = await _db_returning(conn)
    resolved = [
        _var(
            "git_http_auth",
            "github.com",
            source="vcs_connection",
            vcs_connection_id=f"vcs-{uuid.uuid4()}",
            rewrite="none",
        )
    ]
    with patch.object(
        git_auth_service.github_service,
        "get_installation_token",
        new=AsyncMock(side_effect=RuntimeError("github down")),
    ):
        out = await git_auth_service.resolve_git_auth(db, resolved)
    assert out == []  # dropped, not raised


# --- the GitLab switch: a credential that cannot be narrowed ----------------
#
# A GitLab VCS connection stores a Personal or Group Access Token an operator
# pasted in. Nothing produces a narrower copy of one, so the only choices are
# handing it over whole or not at all -- and the connection is named in a
# variable VALUE, so the chooser is whoever can set a workspace variable. Hence
# a switch, off by default, and a refusal rather than a silent drop.


def _gitlab_var(key="gitlab.example.com", ref=None):
    return _var(
        "git_http_auth",
        key,
        source="vcs_connection",
        vcs_connection_id=ref or f"vcs-{uuid.uuid4()}",
        rewrite="none",
    )


async def test_the_shipped_default_is_off():
    """The default IS the fix; a test that only toggled it would not notice."""
    assert settings.vcs.gitlab.allow_token_delivery_to_runners is False


async def test_gitlab_connection_is_refused_while_the_switch_is_off():
    conn = MagicMock(provider="gitlab", token="glpat_STORED")
    db = await _db_returning(conn)
    with pytest.raises(git_auth_service.GitAuthRefused) as excinfo:
        await git_auth_service.resolve_git_auth(db, [_gitlab_var()])
    msg = str(excinfo.value)
    # The message has to carry everything the operator needs, because it is the
    # only thing they see: which variable, the key to set, and the way out that
    # needs no switch at all.
    #
    # Pinned by position rather than by containment. `"gitlab.example.com" in msg`
    # passed for the wrong reasons: it would also hold if the message named
    # `evil-gitlab.example.com`, and a bare host substring test is the shape of an
    # authorization check, so CodeQL flags it as incomplete URL sanitization —
    # correctly, since the pattern is unsafe wherever it decides something.
    assert msg.startswith(f"git credential {'gitlab.example.com'!r} references GitLab ")
    assert "api.config.vcs.gitlab.allow_token_delivery_to_runners" in msg
    assert "static" in msg
    # And never the credential itself -- this string becomes the run's error
    # message, which is rendered in the UI and read back over the API.
    assert "glpat_STORED" not in msg


async def test_a_refusal_delivers_nothing_at_all_rather_than_the_rest():
    """A refusal is fatal, not a drop of the one entry.

    The gate sits BEFORE the `try` that turns every exception in the mint into a
    dropped entry. Moved inside it, this run would be delivered the static
    credential and proceed without the GitLab one -- which is the silent
    half-configured state the switch exists to replace, and `init` would fail
    later naming neither the credential nor the cause.
    """
    conn = MagicMock(provider="gitlab", token="glpat_STORED")
    db = await _db_returning(conn)
    resolved = [
        _var("git_http_auth", "github.com/org", source="static", token="ghp_X", rewrite="none"),
        _gitlab_var(),
    ]
    with pytest.raises(git_auth_service.GitAuthRefused):
        await git_auth_service.resolve_git_auth(db, resolved)


async def test_the_switch_does_not_touch_github():
    """GitHub needs no switch: its token is minted per run and already narrowed.

    Run with the switch at its shipped default (off), so a gate written on the
    `vcs_connection` source rather than on the provider fails here.
    """
    conn = MagicMock(provider="github")
    db = await _db_returning(conn)
    resolved = [_gitlab_var(key="github.com/org")]
    with patch.object(
        git_auth_service.github_service,
        "get_installation_token",
        new=AsyncMock(return_value="ghs_MINTED"),
    ):
        out = await git_auth_service.resolve_git_auth(db, resolved)
    assert json.loads(out[0]["value"])["token"] == "ghs_MINTED"


async def test_an_unknown_provider_is_still_dropped_not_refused():
    """The refusal is specific to GitLab, and the drop paths are unchanged."""
    conn = MagicMock(provider="bitbucket")
    db = await _db_returning(conn)
    assert await git_auth_service.resolve_git_auth(db, [_gitlab_var()]) == []


pytestmark = pytest.mark.asyncio


class TestTheAllowlistBoundsTheCredentialNotTheWorkspaceRepo:
    """GHSA-v8g7-pqrj-8mcm, the run-time half — and the half that was wrong.

    A minted `git_http_auth` credential is installed by the runner at the scope in
    the variable's **key**, as `[credential "https://<key>"]`, which git applies by
    host and path prefix. So the key is what decides how much the token reaches, and
    it is chosen by whoever can set a workspace variable.

    The first version of this check said all of that in its comment and then passed
    `workspace.vcs_repo_url` to `repository_allowed`, which bounds the workspace's own
    repository — already checked at create, at PATCH and at the config fetch. So a
    narrowed connection still minted a host-wide token, and separately a workspace
    with no repository URL of its own (the normal shape for one that mints a
    credential purely for private module sources) was refused outright.
    """

    @staticmethod
    def _ws(repo_url="https://github.com/myorg/thing", conn_id=None):
        return MagicMock(
            vcs_repo_url=repo_url,
            vcs_connection_id=conn_id,
            owner_email="owner@example.com",
        )

    async def _mint(self, key, allowed, *, repo_url="https://github.com/myorg/thing"):
        cid = uuid.uuid4()
        # A REAL VCSConnection, not a MagicMock: `credential_scope_host_allowed` reads
        # `server_url`, and a Mock's attribute is a Mock — which is exactly how a
        # fixture stops resembling the thing it stands in for.
        from terrapod.db.models import VCSConnection

        conn = VCSConnection(id=cid, provider="github", server_url="", allowed_repositories=allowed)
        db = await _db_returning(conn)
        resolved = [
            _var(
                "git_http_auth",
                key,
                source="vcs_connection",
                vcs_connection_id=f"vcs-{cid}",
                rewrite="none",
            )
        ]
        with patch.object(
            git_auth_service.github_service,
            "get_installation_token",
            new=AsyncMock(return_value="ghs_MINTED"),
        ):
            return await git_auth_service.resolve_git_auth(
                db, resolved, workspace=self._ws(repo_url, cid)
            )

    async def test_a_host_wide_key_is_refused_by_a_narrowed_connection(self):
        """The finding. The workspace's own repo IS in the allowlist, so the old
        check passed — and installed a token for the whole of github.com."""
        with pytest.raises(git_auth_service.GitAuthRefused) as exc:
            await self._mint("github.com", ["myorg/*"])
        detail = str(exc.value)
        # What distinguishes the credential-scope refusal from the plain repository
        # one, plus the allowlist it was judged against. Deliberately NOT
        # `"github.com" in detail`: a substring test against a host would also pass on
        # `notgithub.com`, and it is flagged as incomplete URL sanitization — a true
        # positive about the shape of the assertion even in a test. The scope itself is
        # asserted exactly in `TestTheCredentialScopeRefusalNamesTheKey` below.
        assert "a credential installed for" in detail
        assert "path prefix" in detail
        assert "myorg/*" in detail

    async def test_a_key_inside_the_allowlist_is_minted(self):
        out = await self._mint("github.com/myorg", ["myorg/*"])
        assert json.loads(out[0]["value"])["token"] == "ghs_MINTED"

    async def test_an_exact_repo_key_is_minted(self):
        out = await self._mint("github.com/myorg/safe", ["myorg/safe"])
        assert json.loads(out[0]["value"])["token"] == "ghs_MINTED"

    async def test_a_key_for_another_owner_is_refused(self):
        with pytest.raises(git_auth_service.GitAuthRefused):
            await self._mint("github.com/otherorg", ["myorg/*"])

    async def test_an_owner_wide_key_against_a_single_repo_pattern_is_refused(self):
        """`myorg/safe` does not entitle a credential covering all of `myorg`."""
        with pytest.raises(git_auth_service.GitAuthRefused):
            await self._mint("github.com/myorg", ["myorg/safe"])

    async def test_an_empty_allowlist_mints_anything(self):
        """Every deployment that has not opted in, which must be unaffected."""
        out = await self._mint("github.com", [])
        assert json.loads(out[0]["value"])["token"] == "ghs_MINTED"

    async def test_a_workspace_with_no_repo_url_is_not_refused_outright(self):
        """The over-refusal the old subject caused: `repository_allowed("")` is False,
        so a module-sources-only workspace could mint nothing at all."""
        out = await self._mint("github.com/myorg", ["myorg/*"], repo_url="")
        assert json.loads(out[0]["value"])["token"] == "ghs_MINTED"


class TestTheCredentialScopeRefusalNamesTheKey:
    """Exact, rather than by substring: the operator cannot narrow a key they are not
    told about, and the remedy differs from the plain repository refusal's."""

    async def test_the_detail_names_the_scope_and_the_patterns(self):
        import uuid as _uuid

        from terrapod.services.vcs_connection_rbac import credential_scope_refusal_detail

        cid = _uuid.uuid4()
        detail = credential_scope_refusal_detail(cid, "example.invalid", ["myorg/*"])
        assert f"vcs-{cid}" in detail
        assert "'example.invalid'" in detail
        assert "'myorg/*'" in detail
        assert "path prefix" in detail

    async def test_it_says_how_many_more_patterns_there_are(self):
        import uuid as _uuid

        from terrapod.services.vcs_connection_rbac import credential_scope_refusal_detail

        detail = credential_scope_refusal_detail(
            _uuid.uuid4(), "example.invalid", [f"org{i}/*" for i in range(8)]
        )
        assert "(and 3 more)" in detail
