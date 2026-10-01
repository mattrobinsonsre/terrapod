"""Tests for GitHub service — pure-logic functions and mocked HTTP calls."""

import hashlib
import hmac
from unittest.mock import MagicMock, patch

import jwt
import pytest

from terrapod.services.github_service import (
    _api_url,
    _generate_app_jwt,
    _private_key,
    parse_repo_url,
    validate_webhook_signature,
)


def _mock_conn(**overrides):
    conn = MagicMock()
    conn.server_url = overrides.get("server_url", "")
    conn.token = overrides.get("token", "fake-pem-key")
    conn.github_app_id = overrides.get("github_app_id", 12345)
    conn.github_installation_id = overrides.get("github_installation_id", 67890)
    return conn


# ── parse_repo_url ───────────────────────────────────────────────────


class TestParseRepoUrl:
    def test_https_standard(self):
        assert parse_repo_url("https://github.com/owner/repo") == ("owner", "repo")

    def test_https_with_git_suffix(self):
        assert parse_repo_url("https://github.com/owner/repo.git") == ("owner", "repo")

    def test_ssh_format(self):
        assert parse_repo_url("git@github.com:owner/repo.git") == ("owner", "repo")

    def test_ssh_without_git_suffix(self):
        assert parse_repo_url("git@github.com:owner/repo") == ("owner", "repo")

    def test_github_enterprise(self):
        result = parse_repo_url("https://github.acme.com/org/infra")
        assert result == ("org", "infra")

    def test_invalid_url_returns_none(self):
        assert parse_repo_url("not-a-url") is None

    def test_empty_string_returns_none(self):
        assert parse_repo_url("") is None

    def test_whitespace_stripped(self):
        assert parse_repo_url("  https://github.com/owner/repo  ") == ("owner", "repo")

    def test_single_segment_returns_none(self):
        assert parse_repo_url("https://github.com/owner") is None


# ── validate_webhook_signature ───────────────────────────────────────


class TestValidateWebhookSignature:
    @patch("terrapod.services.github_service.settings")
    def test_valid_signature(self, mock_settings):
        secret = "test-webhook-secret"
        mock_settings.vcs.github.webhook_secret = secret
        payload = b'{"action": "push"}'
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        sig_header = f"sha256={expected}"

        assert validate_webhook_signature(payload, sig_header) is True

    @patch("terrapod.services.github_service.settings")
    def test_invalid_signature(self, mock_settings):
        mock_settings.vcs.github.webhook_secret = "real-secret"
        payload = b'{"action": "push"}'

        assert validate_webhook_signature(payload, "sha256=deadbeef") is False

    @patch("terrapod.services.github_service.settings")
    def test_no_secret_configured(self, mock_settings):
        mock_settings.vcs.github.webhook_secret = ""

        assert validate_webhook_signature(b"payload", "sha256=abc") is False

    @patch("terrapod.services.github_service.settings")
    def test_bad_signature_prefix(self, mock_settings):
        mock_settings.vcs.github.webhook_secret = "secret"

        assert validate_webhook_signature(b"payload", "sha1=abc") is False


# ── _generate_app_jwt ────────────────────────────────────────────────


class TestGenerateAppJwt:
    def test_claims_structure(self):
        """JWT contains iat, exp, iss claims with correct app ID."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        # Generate a real RSA key for JWT signing
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        token = _generate_app_jwt(42, pem)
        # Decode without verification to inspect claims — this is a test that
        # asserts the claims our own signer produced; there is no untrusted
        # token here and nothing to verify against.
        # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        claims = jwt.decode(token, options={"verify_signature": False})

        assert claims["iss"] == "42"
        assert "iat" in claims
        assert "exp" in claims

    def test_expiry_is_10_minutes(self):
        """JWT expires ~10 minutes after issuance."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        token = _generate_app_jwt(1, pem)
        # Inspect-only decode of a token we just signed (see above).
        # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
        claims = jwt.decode(token, options={"verify_signature": False})

        # exp should be ~11 minutes from iat (iat is now-60, exp is now+600)
        assert claims["exp"] - claims["iat"] == 11 * 60


# ── _api_url ─────────────────────────────────────────────────────────


class TestApiUrl:
    def test_default_github_api(self):
        conn = _mock_conn(server_url="")
        assert _api_url(conn) == "https://api.github.com"

    def test_custom_ghe_url(self):
        conn = _mock_conn(server_url="https://github.acme.com/api/v3/")
        assert _api_url(conn) == "https://github.acme.com/api/v3"

    def test_trailing_slash_stripped(self):
        conn = _mock_conn(server_url="https://api.github.com/")
        assert _api_url(conn) == "https://api.github.com"


# ── _private_key ─────────────────────────────────────────────────────


class TestPrivateKey:
    def test_returns_token_value(self):
        conn = _mock_conn(
            token="-----BEGIN RSA PRIVATE KEY-----\nfoo\n-----END RSA PRIVATE KEY-----"
        )
        assert _private_key(conn).startswith("-----BEGIN RSA PRIVATE KEY-----")

    def test_raises_when_no_token(self):
        conn = _mock_conn(token="")
        with pytest.raises(ValueError, match="no private key"):
            _private_key(conn)


# ── get_changed_files (mocked HTTP) ──────────────────────────────────


class TestGetChangedFiles:
    @pytest.mark.asyncio
    @patch("terrapod.services.github_service.get_installation_token")
    @patch("terrapod.services.github_service._github_request")
    async def test_returns_none_when_300_plus_files(self, mock_request, mock_token):
        """When GitHub returns 300+ files (truncated), returns None."""
        mock_token.return_value = "fake-token"
        files = [{"filename": f"file{i}.tf"} for i in range(300)]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": files}
        mock_response.raise_for_status = MagicMock()
        mock_request.return_value = mock_response

        from terrapod.services.github_service import get_changed_files

        result = await get_changed_files(_mock_conn(), "owner", "repo", "base", "head")

        assert result is None

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service.get_installation_token")
    @patch("terrapod.services.github_service._github_request")
    async def test_returns_filenames_under_300(self, mock_request, mock_token):
        """When under 300 files, returns the filename list."""
        mock_token.return_value = "fake-token"
        files = [{"filename": "main.tf"}, {"filename": "vars.tf"}]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": files}
        mock_response.raise_for_status = MagicMock()
        mock_request.return_value = mock_response

        from terrapod.services.github_service import get_changed_files

        result = await get_changed_files(_mock_conn(), "owner", "repo", "base", "head")

        assert result == ["main.tf", "vars.tf"]


# ── _github_request retry on 429 / 5xx / secondary-rate-limit 403 ────


def _fake_resp(
    status_code: int,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> MagicMock:
    """Response double that mimics httpx.Response fields used by the retry helper."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.content = content
    return resp


def _patched_client(sequence):
    """Patch httpx.AsyncClient to return responses (or raise exceptions) in order."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.request = AsyncMock(side_effect=list(sequence))
    return mock_client


class TestGithubRequestRetry:
    @pytest.mark.asyncio
    async def test_429_triggers_retry_then_succeeds(self):
        """A 429 with Retry-After is retried after the indicated delay."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        first = _fake_resp(429, {"Retry-After": "1"})
        second = _fake_resp(200)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()) as mock_sleep,
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is second
        assert mock_sleep.await_count == 1
        assert mock_sleep.await_args[0][0] == 1.0

    @pytest.mark.asyncio
    async def test_403_secondary_rate_limit_retries(self):
        """A JSON 403 whose body contains 'secondary rate limit' is retried."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        first = _fake_resp(
            403,
            {"Retry-After": "1", "Content-Type": "application/json; charset=utf-8"},
            b'{"message":"You have exceeded a secondary rate limit."}',
        )
        second = _fake_resp(200)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is second

    @pytest.mark.asyncio
    async def test_403_with_non_text_body_not_treated_as_rate_limit(self):
        """A 403 on a tarball download (binary content-type) should NOT
        be decoded as text, and should not trigger a retry absent the
        rate-limit headers."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        # Large binary body — a real tarball 403 has this shape. The old
        # code would decode it all just to substring-search.
        binary_body = b"\x1f\x8b\x08" + b"\x00" * 10_000_000  # 10 MB of bytes
        binary_403 = _fake_resp(
            403,
            {"Content-Type": "application/x-gzip"},
            binary_body,
        )

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([binary_403])) as m_cls,
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/tarball", "tok", conn=_mock_conn()
            )

        assert resp is binary_403
        # Only one request — not retried
        assert m_cls.return_value.request.await_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retries_for_GET(self):
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        first = _fake_resp(503)
        second = _fake_resp(200)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is second

    @pytest.mark.asyncio
    async def test_5xx_does_not_retry_for_POST(self):
        """POST isn't idempotent — a 502 after a PR comment was written
        could duplicate it on retry. Return the 5xx unretried."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        failure = _fake_resp(502)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([failure])) as m_cls,
        ):
            resp = await _github_request(
                "POST", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is failure
        assert m_cls.return_value.request.await_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retries_for_POST_when_retry_5xx_opt_in(self):
        """Opt-in override: the installation-token endpoint (POST but safe
        to replay) uses retry_5xx=True and must retry on a transient 5xx."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        first = _fake_resp(502)
        second = _fake_resp(201)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _github_request(
                "POST", "https://api.github.com/x", "tok", retry_5xx=True, conn=_mock_conn()
            )

        assert resp is second

    @pytest.mark.asyncio
    async def test_429_still_retried_for_POST(self):
        """429 is a pre-execution rejection — no side effect, safe to
        retry regardless of method."""
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        first = _fake_resp(429, {"Retry-After": "1"})
        second = _fake_resp(201)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([first, second])),
        ):
            resp = await _github_request(
                "POST", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is second

    @pytest.mark.asyncio
    async def test_transport_error_retries(self):
        """httpx transport errors (connect / read timeout) retry, even on POST."""
        from unittest.mock import AsyncMock

        import httpx

        from terrapod.services.github_service import _github_request

        err = httpx.ConnectError("connection refused")
        success = _fake_resp(201)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([err, success])),
        ):
            resp = await _github_request(
                "POST", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is success

    @pytest.mark.asyncio
    async def test_transport_error_exhausted_reraises(self):
        """When retries are exhausted on transport errors, the last exception is raised."""
        from unittest.mock import AsyncMock

        import httpx

        from terrapod.services.github_service import _github_request

        err = httpx.ReadTimeout("read timed out")

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch(
                "httpx.AsyncClient",
                return_value=_patched_client([err, err, err, err]),
            ),
            pytest.raises(httpx.ReadTimeout),
        ):
            await _github_request("GET", "https://api.github.com/x", "tok", conn=_mock_conn())

    @pytest.mark.asyncio
    async def test_retries_exhausted_returns_last_response(self):
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        stuck = _fake_resp(429, {"Retry-After": "1"})

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch(
                "httpx.AsyncClient",
                return_value=_patched_client([stuck, stuck, stuck, stuck]),
            ) as m_cls,
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is stuck
        assert m_cls.return_value.request.await_count == 4

    @pytest.mark.asyncio
    async def test_non_retryable_returns_immediately(self):
        from unittest.mock import AsyncMock

        from terrapod.services.github_service import _github_request

        notfound = _fake_resp(404)

        with (
            patch("terrapod.services.github_service.asyncio.sleep", new=AsyncMock()),
            patch("httpx.AsyncClient", return_value=_patched_client([notfound])) as m_cls,
        ):
            resp = await _github_request(
                "GET", "https://api.github.com/x", "tok", conn=_mock_conn()
            )

        assert resp is notfound
        assert m_cls.return_value.request.await_count == 1


class TestParseRetryDelay:
    def test_prefers_retry_after_seconds(self):
        from terrapod.services.github_service import _parse_retry_delay

        resp = MagicMock(headers={"Retry-After": "7", "X-RateLimit-Reset": "0"})
        assert _parse_retry_delay(resp) == 7.0

    def test_clamps_to_max(self):
        from terrapod.services.github_service import (
            _MAX_RETRY_WAIT_SECONDS,
            _parse_retry_delay,
        )

        resp = MagicMock(headers={"Retry-After": "3600"})
        assert _parse_retry_delay(resp) == _MAX_RETRY_WAIT_SECONDS

    def test_falls_back_to_default(self):
        from terrapod.services.github_service import (
            _DEFAULT_BACKOFF_SECONDS,
            _parse_retry_delay,
        )

        resp = MagicMock(headers={})
        assert _parse_retry_delay(resp) == _DEFAULT_BACKOFF_SECONDS


@pytest.fixture(scope="module")
def app_pem() -> str:
    """A real app key, so the whole mint path runs rather than a patched middle."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


class TestAMintedTokenIsNoWiderThanItsPurpose:
    """A mint with no `permissions` body carries every permission the App holds,
    across every repository in the installation.

    That token does not stay inside the API process: `git_auth_service` writes it
    into a runner Job's git credential helper and `git_fetch` hands it to `git`
    as a Basic header, in a Job that is also running user-supplied IaC. A clone
    needs `contents: read`; anything beyond that is handed over for free.

    `repositories` is deliberately not narrowed too — see `CLONE_PERMISSIONS`.
    """

    @staticmethod
    def _mint_body(mock_request) -> dict | None:
        """The `json=` the access-token POST actually sent, if any."""
        return mock_request.call_args.kwargs.get("json")

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from terrapod.services import github_service

        github_service._token_cache.clear()  # noqa: SLF001
        yield
        github_service._token_cache.clear()  # noqa: SLF001

    def _token_response(self):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"token": "ghs_minted"}
        resp.raise_for_status = MagicMock()
        return resp

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service._github_request")
    async def test_the_default_asks_for_contents_read_and_nothing_else(self, mock_request, app_pem):
        """The default is what the out-of-module callers get, so it must be the
        narrow one — they do not pass `permissions` and cannot be expected to."""
        from terrapod.services.github_service import get_installation_token

        mock_request.return_value = self._token_response()
        assert await get_installation_token(_mock_conn(token=app_pem)) == "ghs_minted"

        assert self._mint_body(mock_request) == {"permissions": {"contents": "read"}}

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service._github_request")
    async def test_terrapods_own_api_calls_still_get_the_full_app_grant(
        self, mock_request, app_pem
    ):
        """Negative path. The internal token writes PR comments, commit statuses
        and merges; naming each permission risks a 422 on an installation that
        grants a different one, which would take out every VCS operation rather
        than one. The body is OMITTED — GitHub reads absence as the full grant,
        where an explicit `{}` would mint a powerless token.
        """
        from terrapod.services.github_service import _api_call_token

        mock_request.return_value = self._token_response()
        assert await _api_call_token(_mock_conn(token=app_pem)) == "ghs_minted"

        assert self._mint_body(mock_request) is None

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service._github_request")
    async def test_a_write_operation_does_not_run_on_the_clone_token(self, mock_request, app_pem):
        """Driven through a real caller rather than asserted on the helper: an
        internal operation that reached for the default would 403 in the field.
        """
        from terrapod.services.github_service import create_pr_comment

        token_resp = self._token_response()
        comment_resp = MagicMock()
        comment_resp.status_code = 201
        comment_resp.json.return_value = {"id": 7}
        comment_resp.raise_for_status = MagicMock()
        mock_request.side_effect = [token_resp, comment_resp]

        await create_pr_comment(_mock_conn(token=app_pem), "acme", "infra", 1, "hello")

        mint = mock_request.call_args_list[0]
        assert "access_tokens" in mint.args[1]
        assert mint.kwargs.get("json") is None, (
            "a PR comment ran on a contents:read token — it will 403 in the field"
        )

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service._github_request")
    async def test_the_cache_does_not_serve_one_permission_set_for_another(
        self, mock_request, app_pem
    ):
        """The cache key includes the permission set.

        Keyed on the installation alone, whichever call warmed it first would win:
        a clone would be handed the fully-permissioned token — restoring the
        vulnerability through the cache — or a write would be handed the
        read-only one and 403 depending on call order.
        """
        from terrapod.services.github_service import _api_call_token, get_installation_token

        clone_resp = self._token_response()
        clone_resp.json.return_value = {"token": "ghs_clone"}
        api_resp = self._token_response()
        api_resp.json.return_value = {"token": "ghs_api"}
        mock_request.side_effect = [clone_resp, api_resp]

        conn = _mock_conn(token=app_pem)
        assert await get_installation_token(conn) == "ghs_clone"
        assert await _api_call_token(conn) == "ghs_api"
        assert mock_request.call_count == 2, "the second mint was served from the first's cache"

        # And each is still cached in its own right — the split must not cost a
        # mint per call.
        assert await get_installation_token(conn) == "ghs_clone"
        assert await _api_call_token(conn) == "ghs_api"
        assert mock_request.call_count == 2
