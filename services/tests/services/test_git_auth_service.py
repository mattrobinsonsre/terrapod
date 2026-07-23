"""Tests for git-auth source resolution (#1028).

The server resolves each git-auth variable's *source* into a concrete credential
before delivery, so the runner phase is source-agnostic. Static values pass
through; a ``vcs_connection`` source mints a short-lived token from the referenced
VCS connection. A cred that can't be resolved is dropped (never fails the run).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    conn = MagicMock(provider="gitlab", token="glpat_STORED")
    db = await _db_returning(conn)
    resolved = [
        _var(
            "git_http_auth",
            "gitlab.example.com",
            source="vcs_connection",
            vcs_connection_id=f"vcs-{uuid.uuid4()}",
            rewrite="none",
        )
    ]
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


pytestmark = pytest.mark.asyncio
