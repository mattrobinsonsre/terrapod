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
        # Decode without verification to inspect claims
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
    async def test_returns_none_when_300_plus_files(self, mock_token):
        """When GitHub returns 300+ files (truncated), returns None."""
        from unittest.mock import AsyncMock

        mock_token.return_value = "fake-token"
        files = [{"filename": f"file{i}.tf"} for i in range(300)]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": files}
        mock_response.raise_for_status = MagicMock()

        conn = _mock_conn()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            from terrapod.services.github_service import get_changed_files

            result = await get_changed_files(conn, "owner", "repo", "base", "head")

        assert result is None

    @pytest.mark.asyncio
    @patch("terrapod.services.github_service.get_installation_token")
    async def test_returns_filenames_under_300(self, mock_token):
        """When under 300 files, returns the filename list."""
        from unittest.mock import AsyncMock

        mock_token.return_value = "fake-token"
        files = [{"filename": "main.tf"}, {"filename": "vars.tf"}]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": files}
        mock_response.raise_for_status = MagicMock()

        conn = _mock_conn()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            from terrapod.services.github_service import get_changed_files

            result = await get_changed_files(conn, "owner", "repo", "base", "head")

        assert result == ["main.tf", "vars.tf"]
