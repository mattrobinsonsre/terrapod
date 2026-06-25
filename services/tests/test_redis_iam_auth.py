"""Tests for cloud-IAM Redis/Valkey auth (#579) — AWS ElastiCache, GCP
Memorystore, Azure Cache for Redis.

The live connection per cloud can only be validated against a real IAM-enabled
cache (a staging smoke); these tests cover the unit logic — per-cloud token
minting, URL-credential stripping, the credential provider (sync + async)
dispatch, and the redis client wiring — and guard that the default stays the
static auth string.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import RedisConfig
from terrapod.redis import iam_auth


def test_auth_mode_defaults_to_password():
    # Static auth-string Redis auth must remain the default.
    assert RedisConfig().auth_mode == "password"


def test_strip_url_credentials_removes_userinfo():
    assert (
        iam_auth.strip_url_credentials("rediss://user:pass@cache.example.com:6379/0")
        == "rediss://cache.example.com:6379/0"
    )
    # No userinfo → unchanged (scheme/host/port preserved).
    assert (
        iam_auth.strip_url_credentials("rediss://cache.example.com:6379")
        == "rediss://cache.example.com:6379"
    )


def test_mint_aws_elasticache_token_signs_and_strips_scheme(monkeypatch):
    mock_signer = MagicMock()
    mock_signer.generate_presigned_url.return_value = (
        "https://my-cache/?Action=connect&User=terrapod&X-Amz-Signature=abc"
    )
    monkeypatch.setattr(iam_auth, "_aws_signer", lambda region: mock_signer)

    tok = iam_auth.mint_aws_elasticache_token(
        cache_name="my-cache", user="terrapod", region="us-east-1"
    )

    # Token is the presigned URL without the scheme.
    assert tok == "my-cache/?Action=connect&User=terrapod&X-Amz-Signature=abc"
    args = mock_signer.generate_presigned_url.call_args
    assert args.kwargs["operation_name"] == "connect"
    assert "Action=connect" in args.args[0]["url"]
    assert "User=terrapod" in args.args[0]["url"]


# ── credential provider dispatch ──────────────────────────────────────


def _creds(provider):
    return (provider.get_credentials(), asyncio.run(provider.get_credentials_async()))


def test_provider_aws_returns_username_and_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_aws_elasticache_token", lambda **_kw: "AWS")
    p = iam_auth.make_credential_provider(
        auth_mode="aws_iam", username="terrapod", cache_name="c", region="r"
    )
    sync, asyncc = _creds(p)
    assert sync == ("terrapod", "AWS")
    assert asyncc == ("terrapod", "AWS")


def test_provider_gcp_uses_access_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_gcp_access_token", lambda: "GCP")
    p = iam_auth.make_credential_provider(
        auth_mode="gcp_iam", username="sa@proj.iam", cache_name="", region=""
    )
    assert asyncio.run(p.get_credentials_async()) == ("sa@proj.iam", "GCP")


def test_provider_azure_uses_entra_token(monkeypatch):
    monkeypatch.setattr(iam_auth, "mint_azure_redis_token", lambda: "AZURE")
    p = iam_auth.make_credential_provider(
        auth_mode="azure_ad", username="obj-id", cache_name="", region=""
    )
    assert asyncio.run(p.get_credentials_async()) == ("obj-id", "AZURE")


def test_provider_mints_fresh_token_each_call(monkeypatch):
    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(iam_auth, "mint_aws_elasticache_token", lambda **_kw: next(tokens))
    p = iam_auth.make_credential_provider(
        auth_mode="aws_iam", username="u", cache_name="c", region="r"
    )
    assert p.get_credentials()[1] == "t1"
    assert p.get_credentials()[1] == "t2"


def test_provider_unsupported_mode_raises():
    with pytest.raises(ValueError, match="unsupported IAM redis"):
        iam_auth.make_credential_provider(auth_mode="nope", username="u", cache_name="", region="")


# ── redis client wiring ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_redis_uses_credential_provider_only_for_iam_mode():
    """init_redis passes a credential_provider for IAM modes, not for password."""
    from terrapod.redis import client as redis_client

    async def _check(auth_mode: str, *, expect_provider: bool):
        cfg = RedisConfig(auth_mode=auth_mode, username="terrapod", aws_cache_name="c")
        fake_redis = MagicMock()
        fake_redis.ping = AsyncMock()
        fake_redis.aclose = AsyncMock()
        with (
            patch.object(redis_client, "settings") as fake_settings,
            patch.object(redis_client.aioredis, "from_url", return_value=fake_redis) as from_url,
        ):
            fake_settings.redis = cfg
            fake_settings.redis_url = "rediss://user:pw@cache.example.com:6379"
            await redis_client.init_redis()
            kwargs = from_url.call_args.kwargs
            if expect_provider:
                assert "credential_provider" in kwargs
                # URL userinfo stripped (provider supplies username + token).
                assert "@" not in from_url.call_args.args[0]
            else:
                assert "credential_provider" not in kwargs
        await redis_client.close_redis()

    await _check("password", expect_provider=False)
    await _check("aws_iam", expect_provider=True)
