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
import gc
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
    # Username-only + db-number preserved.
    assert (
        iam_auth.strip_url_credentials("rediss://user@cache.example.com:6379/2")
        == "rediss://cache.example.com:6379/2"
    )


def test_strip_url_credentials_rebrackets_ipv6():
    # IPv6 literal must stay bracketed or the port can't be parsed.
    assert (
        iam_auth.strip_url_credentials("rediss://u:p@[2001:db8::1]:6379/0")
        == "rediss://[2001:db8::1]:6379/0"
    )


def test_redis_config_requires_username_for_iam():
    with pytest.raises(ValueError, match="redis.username is required"):
        RedisConfig(auth_mode="gcp_iam")


def test_redis_config_requires_cache_name_for_aws():
    with pytest.raises(ValueError, match="aws_cache_name is required"):
        RedisConfig(auth_mode="aws_iam", username="terrapod")


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


def _fake_aws_env(monkeypatch):
    """Isolate the two AWS tests below that build a **real** botocore signer.

    Signing is local arithmetic — nothing here reaches AWS, and the credentials
    are never validated — but botocore still needs *some* credentials to sign
    with. Every ambient source is cleared so the result does not depend on
    whether the machine running the tests happens to have AWS config: with a key
    and secret in the environment botocore's ``EnvProvider`` wins its resolution
    chain outright, so it never consults the filesystem, ECS or IMDS.
    """
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        # Left set, botocore's EnvProvider hands back RefreshableCredentials
        # whose refresher re-reads this same stale value, and signing dies with
        # "credentials are still expired". `aws configure export-credentials`
        # emits it, so a developer can easily have one exported.
        "AWS_CREDENTIAL_EXPIRATION",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(iam_auth, "_aws_signers", {})  # isolate the module cache


def test_the_signer_is_cached_per_region(monkeypatch):
    """The cache itself is load-bearing, and was otherwise unguarded (#1509).

    `_aws_signer` states twice that the signer must not be rebuilt per call, but
    nothing tested it: removing the cache outright left every other test in this
    file green, because a signer used inside the call that built it never
    outlives its session. That the session is *retained* is proved by
    ``test_aws_token_still_mints_after_the_session_could_be_collected`` — an
    assertion here that the cached session is still reachable would be vacuous,
    since the cache holds it strongly for the whole test either way.
    """
    _fake_aws_env(monkeypatch)

    # Cached, not rebuilt per connection: a Redis reconnect must not pay for a
    # fresh botocore session, and the retained signer is what carries
    # refreshable credentials across rotation.
    assert iam_auth._aws_signer("us-east-1") is iam_auth._aws_signer("us-east-1")
    # Keyed by region, so two regions do not share one signer.
    assert iam_auth._aws_signer("us-east-1") is not iam_auth._aws_signer("eu-west-1")


def test_aws_token_mints_when_the_region_is_left_unset(monkeypatch):
    """`redis.aws_iam_region: ""` is a supported (and reported) configuration.

    It takes the other branch of `_aws_signer` — the region comes from the
    session's own config and the cache key becomes "default" — so it is the
    branch a deployment that omits the setting actually runs.
    """
    _fake_aws_env(monkeypatch)

    token = iam_auth.mint_aws_elasticache_token(cache_name="my-cache", user="terrapod", region="")

    assert token.startswith("my-cache/?")
    assert "X-Amz-Signature=" in token
    assert set(iam_auth._aws_signers) == {"default"}


def test_aws_token_still_mints_after_the_session_could_be_collected(monkeypatch):
    """The signer must outlive the botocore session that built it (#1509).

    This is the one AWS test that builds a **real** ``RequestSigner`` rather
    than mocking ``_aws_signer``. That mocking is why the bug shipped: every
    other test replaces the very construction that was wrong.

    ``RequestSigner`` keeps the event emitter as a ``weakref.proxy`` and the
    session is what holds it strongly, so a session dropped on return is
    collected and later signing raises ``ReferenceError``. In production the
    first connection succeeded (it beat the collector) and everything after it
    failed, which read as an intermittent auth fault.

    The explicit ``gc.collect()`` is load-bearing: without it this passes
    against the broken code too, because the session simply hadn't been
    collected yet.
    """
    _fake_aws_env(monkeypatch)

    first = iam_auth.mint_aws_elasticache_token(
        cache_name="my-cache", user="terrapod", region="us-east-1"
    )
    assert "X-Amz-Signature=" in first

    gc.collect()

    second = iam_auth.mint_aws_elasticache_token(
        cache_name="my-cache", user="terrapod", region="us-east-1"
    )
    assert second.startswith("my-cache/?")
    assert "X-Amz-Signature=" in second


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
                # password mode passes the URL through untouched (userinfo kept).
                assert from_url.call_args.args[0] == fake_settings.redis_url
        await redis_client.close_redis()

    await _check("password", expect_provider=False)
    await _check("aws_iam", expect_provider=True)


@pytest.mark.asyncio
async def test_init_redis_iam_requires_tls():
    """IAM modes refuse a non-TLS (redis://) URL — tokens never go plaintext."""
    from terrapod.redis import client as redis_client

    cfg = RedisConfig(auth_mode="aws_iam", username="terrapod", aws_cache_name="c")
    with (
        patch.object(redis_client, "settings") as fake_settings,
        patch.object(redis_client.aioredis, "from_url") as from_url,
    ):
        fake_settings.redis = cfg
        fake_settings.redis_url = "redis://cache.example.com:6379"  # non-TLS
        with pytest.raises(ValueError, match="requires TLS"):
            await redis_client.init_redis()
        from_url.assert_not_called()
