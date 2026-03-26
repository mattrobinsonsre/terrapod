"""Tests for GitLab service — pure-logic functions and mocked HTTP calls."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.gitlab_service import (
    _api_url,
    _project_path,
    _token,
    parse_repo_url,
)


def _mock_conn(**overrides):
    conn = MagicMock()
    conn.server_url = overrides.get("server_url", "")
    conn.token = overrides.get("token", "glpat-fake-token")
    return conn


# ── parse_repo_url ───────────────────────────────────────────────────


class TestParseRepoUrl:
    def test_https_standard(self):
        assert parse_repo_url("https://gitlab.com/group/project") == ("group", "project")

    def test_https_nested_groups(self):
        result = parse_repo_url("https://gitlab.com/group/subgroup/project")
        assert result == ("group/subgroup", "project")

    def test_https_with_git_suffix(self):
        assert parse_repo_url("https://gitlab.com/group/project.git") == ("group", "project")

    def test_ssh_format(self):
        assert parse_repo_url("git@gitlab.com:group/project.git") == ("group", "project")

    def test_ssh_nested_groups(self):
        result = parse_repo_url("git@gitlab.com:group/subgroup/project.git")
        assert result == ("group/subgroup", "project")

    def test_self_hosted(self):
        result = parse_repo_url("https://gitlab.acme.com/infra/terraform")
        assert result == ("infra", "terraform")

    def test_self_hosted_with_git_suffix(self):
        result = parse_repo_url("https://gitlab.acme.com/infra/terraform.git")
        assert result == ("infra", "terraform")

    def test_invalid_url_returns_none(self):
        assert parse_repo_url("not-a-url") is None

    def test_empty_string_returns_none(self):
        assert parse_repo_url("") is None

    def test_single_segment_returns_none(self):
        """A URL with only a hostname and one path segment is not a valid repo URL."""
        assert parse_repo_url("https://gitlab.com/group") is None


# ── _project_path ────────────────────────────────────────────────────


class TestProjectPath:
    def test_url_encodes_slash(self):
        assert _project_path("group", "repo") == "group%2Frepo"

    def test_nested_group(self):
        assert _project_path("group/subgroup", "repo") == "group%2Fsubgroup%2Frepo"


# ── _api_url ─────────────────────────────────────────────────────────


class TestApiUrl:
    def test_default_gitlab_com(self):
        conn = _mock_conn(server_url="")
        assert _api_url(conn) == "https://gitlab.com/api/v4"

    def test_custom_url(self):
        conn = _mock_conn(server_url="https://gitlab.acme.com/")
        assert _api_url(conn) == "https://gitlab.acme.com/api/v4"

    def test_trailing_slash_stripped(self):
        conn = _mock_conn(server_url="https://gitlab.com/")
        assert _api_url(conn) == "https://gitlab.com/api/v4"


# ── _token ───────────────────────────────────────────────────────────


class TestToken:
    def test_returns_token_value(self):
        conn = _mock_conn(token="glpat-abc123")
        assert _token(conn) == "glpat-abc123"

    def test_raises_when_no_token(self):
        conn = _mock_conn(token="")
        with pytest.raises(ValueError, match="no token"):
            _token(conn)


# ── get_changed_files (mocked HTTP) ──────────────────────────────────


class TestGetChangedFiles:
    @pytest.mark.asyncio
    async def test_collects_old_and_new_paths(self):
        """Both old_path and new_path are collected to catch renames."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "diffs": [
                {"old_path": "old/file.tf", "new_path": "new/file.tf"},
                {"old_path": "same.tf", "new_path": "same.tf"},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        conn = _mock_conn()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            from terrapod.services.gitlab_service import get_changed_files

            result = await get_changed_files(conn, "group", "repo", "base", "head")

        assert result is not None
        assert set(result) == {"old/file.tf", "new/file.tf", "same.tf"}

    @pytest.mark.asyncio
    async def test_returns_none_when_500_plus_diffs(self):
        """When GitLab returns 500+ diffs (truncated), returns None."""
        diffs = [{"old_path": f"f{i}.tf", "new_path": f"f{i}.tf"} for i in range(500)]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"diffs": diffs}
        mock_response.raise_for_status = MagicMock()

        conn = _mock_conn()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            from terrapod.services.gitlab_service import get_changed_files

            result = await get_changed_files(conn, "group", "repo", "base", "head")

        assert result is None
