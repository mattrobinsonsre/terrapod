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


# ── _gitlab_request retry on 429 / 5xx / transport (#360) ────────────


def _fake_resp(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


def _patched_client(sequence):
    """Patch httpx.AsyncClient to return responses/raise exceptions in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.request = AsyncMock(side_effect=list(sequence))
    return mock_client


class TestGitlabRequestRetry:
    """Cross-provider parity with `_github_request`. Before #360 GitLab had
    no retry at all — a single 429/5xx silently dropped commit-status
    posts, causing speculative-run "completed" checks to never land."""

    @pytest.mark.asyncio
    async def test_429_with_retry_after_retries_then_succeeds(self):
        from terrapod.services.gitlab_service import _gitlab_request

        first = _fake_resp(429, {"Retry-After": "1"})
        second = _fake_resp(200)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()) as mock_sleep,
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _gitlab_request("POST", "https://gitlab.example/x", _mock_conn())

        assert resp is second
        assert mock_sleep.await_count == 1
        assert mock_sleep.await_args[0][0] == 1.0

    @pytest.mark.asyncio
    async def test_5xx_on_get_retries(self):
        from terrapod.services.gitlab_service import _gitlab_request

        first = _fake_resp(502)
        second = _fake_resp(200)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _gitlab_request("GET", "https://gitlab.example/x", _mock_conn())

        assert resp is second

    @pytest.mark.asyncio
    async def test_5xx_on_post_does_not_retry_by_default(self):
        """POST/PATCH/PUT/DELETE may have had a server-side effect — we don't
        replay them on 5xx unless the caller opts in with retry_5xx=True."""
        from terrapod.services.gitlab_service import _gitlab_request

        only = _fake_resp(503)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([only])) as m_cls,
        ):
            resp = await _gitlab_request("POST", "https://gitlab.example/x", _mock_conn())

        assert resp is only
        assert m_cls.return_value.request.await_count == 1

    @pytest.mark.asyncio
    async def test_5xx_on_post_with_retry_5xx_true_retries(self):
        """create_commit_status opts in to retry_5xx because the status is
        last-write-wins on (sha, name) — replays are idempotent."""
        from terrapod.services.gitlab_service import _gitlab_request

        first = _fake_resp(503)
        second = _fake_resp(200)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _gitlab_request(
                "POST", "https://gitlab.example/x", _mock_conn(), retry_5xx=True
            )

        assert resp is second

    @pytest.mark.asyncio
    async def test_transport_error_retries_then_succeeds(self):
        """Transport-level errors are pre-execution, safe to retry any method."""
        import httpx

        from terrapod.services.gitlab_service import _gitlab_request

        ok = _fake_resp(200)
        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch(
                "httpx.AsyncClient",
                return_value=_patched_client([httpx.ConnectError("boom"), ok]),
            ),
        ):
            resp = await _gitlab_request("POST", "https://gitlab.example/x", _mock_conn())

        assert resp is ok

    @pytest.mark.asyncio
    async def test_retries_exhausted_returns_last_response(self):
        """After _MAX_RETRIES retryable responses, the loop returns the
        last one rather than looping forever."""
        from terrapod.services.gitlab_service import _gitlab_request

        # 4 attempts max (1 initial + 3 retries), all 503 with retry_5xx=True
        responses = [_fake_resp(503) for _ in range(4)]

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()) as mock_sleep,
            patch("httpx.AsyncClient", return_value=_patched_client(responses)) as m_cls,
        ):
            resp = await _gitlab_request(
                "POST", "https://gitlab.example/x", _mock_conn(), retry_5xx=True
            )

        assert resp.status_code == 503
        assert m_cls.return_value.request.await_count == 4  # initial + 3 retries
        assert mock_sleep.await_count == 3  # sleep between each retry, not after last

    @pytest.mark.asyncio
    async def test_4xx_other_than_429_does_not_retry(self):
        """403 / 404 / 422 are caller bugs — no point retrying."""
        from terrapod.services.gitlab_service import _gitlab_request

        only = _fake_resp(404)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([only])) as m_cls,
        ):
            resp = await _gitlab_request("GET", "https://gitlab.example/x", _mock_conn())

        assert resp is only
        assert m_cls.return_value.request.await_count == 1

    @pytest.mark.asyncio
    async def test_429_without_retry_after_falls_back_to_default_backoff(self):
        """A 429 with no Retry-After header still retries — using the
        default backoff so a misbehaving upstream can't tighten our loop."""
        from terrapod.services.gitlab_service import _DEFAULT_BACKOFF_SECONDS, _gitlab_request

        first = _fake_resp(429)  # no Retry-After header
        second = _fake_resp(200)

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()) as mock_sleep,
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _gitlab_request("GET", "https://gitlab.example/x", _mock_conn())

        assert resp is second
        assert mock_sleep.await_count == 1
        assert mock_sleep.await_args[0][0] == _DEFAULT_BACKOFF_SECONDS

    @pytest.mark.asyncio
    async def test_create_commit_status_uses_retry_5xx(self):
        """End-to-end: create_commit_status now survives a transient 502."""
        from terrapod.services.gitlab_service import create_commit_status

        first = _fake_resp(502)
        second = _fake_resp(200)
        second.raise_for_status = MagicMock()

        with (
            patch("terrapod.services.gitlab_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            # Must not raise — the retry kicks in on the 502, second attempt
            # succeeds, and the 200's raise_for_status is a no-op MagicMock.
            await create_commit_status(
                _mock_conn(server_url="https://gitlab.example"),
                "group",
                "project",
                "deadbeef",
                state="success",
                description="ok",
            )
