"""Tests for agent pool service — join token generation and validation."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.agent_pool_service import (
    _LISTENER_FP_PREFIX,
    LISTENER_POD_TTL,
    _fingerprint_ttl,
    _register_fingerprint,
    count_listener_replicas,
    create_pool_token,
    generate_join_token,
    heartbeat_listener,
    is_fingerprint_valid,
    validate_join_token,
)

# ── generate_join_token ──────────────────────────────────────────────


class TestGenerateJoinToken:
    def test_returns_url_safe_token(self):
        """Raw token is URL-safe base64 (no +, /, or = characters)."""
        raw, _ = generate_join_token()
        # URL-safe base64 uses only alphanumerics, hyphens, and underscores
        assert all(c.isalnum() or c in "-_" for c in raw)

    def test_hash_matches_raw(self):
        """SHA-256 hash matches the raw token value."""
        raw, token_hash = generate_join_token()
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert token_hash == expected

    def test_uniqueness(self):
        """Each call produces a different token."""
        tokens = {generate_join_token()[0] for _ in range(100)}
        assert len(tokens) == 100


# ── validate_join_token ──────────────────────────────────────────────


def _mock_token(**overrides):
    """Create a mock AgentPoolToken."""
    token = MagicMock()
    token.is_revoked = overrides.get("is_revoked", False)
    token.expires_at = overrides.get("expires_at", None)
    token.max_uses = overrides.get("max_uses", None)
    token.use_count = overrides.get("use_count", 0)
    return token


class TestValidateJoinToken:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        """A valid, non-expired, non-revoked token returns the record."""
        raw, token_hash = generate_join_token()
        mock_record = _mock_token()

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_unknown_token_returns_none(self):
        """A token not in the database returns None."""
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        result = await validate_join_token(db, "nonexistent-token")
        assert result is None

    @pytest.mark.asyncio
    async def test_revoked_token_returns_none(self):
        """A revoked token returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(is_revoked=True)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self):
        """An expired token returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(expires_at=datetime.now(UTC) - timedelta(hours=1))

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_max_uses_exceeded_returns_none(self):
        """A token that has reached max_uses returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=5, use_count=5)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_expiry_is_valid(self):
        """A token with no expiry (None) is valid."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(expires_at=None)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_none_max_uses_is_valid(self):
        """A token with no max_uses (None) is valid regardless of use_count."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=None, use_count=999)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_under_max_uses_is_valid(self):
        """A token with use_count < max_uses is valid."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=10, use_count=3)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record


# ── create_pool_token defaults ──────────────────────────────────────


def _capture_pool_token_call() -> AsyncMock:
    """Build an AsyncSession mock that records the AgentPoolToken passed to add()."""
    db = AsyncMock()
    captured = {}

    def add(token):
        captured["token"] = token

    db.add = MagicMock(side_effect=add)
    db.flush = AsyncMock()
    db._captured = captured
    return db


class TestCreatePoolToken:
    @pytest.mark.asyncio
    async def test_defaults_max_uses_to_two(self):
        """Default max_uses comes from settings.agent_pools (2 in the bundled config)."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="test",
            created_by="alice@example.com",
        )

        assert db._captured["token"].max_uses == 2

    @pytest.mark.asyncio
    async def test_defaults_expiry_to_one_hour_from_now(self):
        """Default expires_at is now + default_join_token_ttl_seconds (3600)."""
        db = _capture_pool_token_call()

        before = datetime.now(UTC)
        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="test",
            created_by="alice@example.com",
        )
        after = datetime.now(UTC)

        expires_at = db._captured["token"].expires_at
        # Should land within (now+1h - small skew, now+1h + small skew)
        assert before + timedelta(seconds=3590) <= expires_at <= after + timedelta(seconds=3610)

    @pytest.mark.asyncio
    async def test_explicit_max_uses_none_means_unlimited(self):
        """Caller passing max_uses=None (vs default sentinel) opts out of the cap."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="bootstrap",
            created_by="op@example.com",
            max_uses=None,
        )

        assert db._captured["token"].max_uses is None

    @pytest.mark.asyncio
    async def test_explicit_expires_at_none_means_no_expiry(self):
        """Caller passing expires_at=None opts out of the default TTL."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="bootstrap",
            created_by="op@example.com",
            expires_at=None,
        )

        assert db._captured["token"].expires_at is None

    @pytest.mark.asyncio
    async def test_explicit_values_override_defaults(self):
        """Caller-supplied max_uses + expires_at win over the config defaults."""
        db = _capture_pool_token_call()
        custom_expiry = datetime.now(UTC) + timedelta(days=7)

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="weekly-rotated",
            created_by="op@example.com",
            max_uses=10,
            expires_at=custom_expiry,
        )

        assert db._captured["token"].max_uses == 10
        assert db._captured["token"].expires_at == custom_expiry


# ── heartbeat_listener — pod_name + replica tracking ──────────────────


class TestHeartbeatListenerPodTracking:
    @pytest.mark.asyncio
    async def test_heartbeat_with_pod_name_writes_per_pod_key(self):
        """When pod_name is supplied the heartbeat refreshes a tp:listener_pod:{lid}:{pod} TTL key."""
        mock_redis = MagicMock()
        # Pipeline mock: track .set() calls and provide an awaitable execute()
        mock_pipe = MagicMock()
        mock_pipe.hset = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_redis.pipeline.return_value = mock_pipe

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            await heartbeat_listener(
                listener_id="lid-1",
                name="lis",
                pod_name="lis-pod-abc",
                capacity="5",
            )

        # The per-pod key write must use SET with TTL = LISTENER_POD_TTL
        mock_pipe.set.assert_called_once()
        args, kwargs = mock_pipe.set.call_args
        assert args[0] == "tp:listener_pod:lid-1:lis-pod-abc"
        assert kwargs.get("ex") == LISTENER_POD_TTL
        mock_pipe.execute.assert_awaited_once()

        # tracks_pods="1" must be added to the listener hash so the API can
        # tell this listener is on a post-0.19.0 image.
        mock_pipe.hset.assert_called_once()
        hset_kwargs = mock_pipe.hset.call_args.kwargs
        mapping = hset_kwargs.get("mapping") or mock_pipe.hset.call_args.args[1]
        assert mapping.get("tracks_pods") == "1"

    @pytest.mark.asyncio
    async def test_heartbeat_without_pod_name_does_not_set_tracks_pods(self):
        """Without pod_name we don't claim to be tracking pods — the API
        relies on the absence of tracks_pods to omit replica-count."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.hset = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_redis.pipeline.return_value = mock_pipe

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            await heartbeat_listener(listener_id="lid-1", name="lis", capacity="5")

        mapping = mock_pipe.hset.call_args.kwargs.get("mapping") or mock_pipe.hset.call_args.args[1]
        assert "tracks_pods" not in mapping

    @pytest.mark.asyncio
    async def test_heartbeat_without_pod_name_skips_pod_key(self):
        """Older clients (no pod_name) still heartbeat; no per-pod key is written."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.hset = MagicMock()
        mock_pipe.expire = MagicMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_redis.pipeline.return_value = mock_pipe

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            await heartbeat_listener(listener_id="lid-1", name="lis", capacity="5")

        mock_pipe.set.assert_not_called()


# ── count_listener_replicas ───────────────────────────────────────────


class TestCountListenerReplicas:
    @pytest.mark.asyncio
    async def test_counts_pod_keys_via_scan(self):
        """Replica count is the number of tp:listener_pod:{lid}:* keys."""
        mock_redis = MagicMock()

        async def fake_scan_iter(match=None, count=None):
            assert match == "tp:listener_pod:lid-x:*"
            for k in (
                "tp:listener_pod:lid-x:pod-a",
                "tp:listener_pod:lid-x:pod-b",
                "tp:listener_pod:lid-x:pod-c",
            ):
                yield k

        mock_redis.scan_iter = fake_scan_iter

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            count = await count_listener_replicas("lid-x")

        assert count == 3

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_pod_keys(self):
        """No per-pod keys (e.g. listener still on pre-0.19.0 image) → 0."""
        mock_redis = MagicMock()

        async def empty_scan_iter(match=None, count=None):
            return
            yield  # pragma: no cover (make this an async generator)

        mock_redis.scan_iter = empty_scan_iter

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            assert await count_listener_replicas("lid-empty") == 0


# ── Per-fingerprint registration (regression: cert renewal race) ──────


class TestFingerprintRegistration:
    """Each issued cert registers its fingerprint as its own TTL'd key.

    The previous design tracked a single "current" fingerprint on the listener
    hash. When N listener pods all called /renew within the same window, the
    API issued N distinct certs but the K8s Secret CAS picked one — leaving
    Redis pointing at a fingerprint that wasn't necessarily the cert all pods
    actually used. Subsequent cert-auth calls 401'd with "fingerprint mismatch".

    The fix: store each issued fingerprint under its own key. Auth does EXISTS
    instead of equality. Concurrent renewals all stay valid until they expire.
    """

    @pytest.mark.asyncio
    async def test_register_writes_setex_with_namespaced_key(self):
        mock_redis = MagicMock()
        mock_redis.setex = AsyncMock()

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            await _register_fingerprint("lid-x", "fp-abc", 600)

        mock_redis.setex.assert_awaited_once_with(f"{_LISTENER_FP_PREFIX}lid-x:fp-abc", 600, "1")

    @pytest.mark.asyncio
    async def test_is_valid_true_when_key_exists(self):
        mock_redis = MagicMock()
        mock_redis.exists = AsyncMock(return_value=1)

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            assert await is_fingerprint_valid("lid-x", "fp-abc") is True

        mock_redis.exists.assert_awaited_once_with(f"{_LISTENER_FP_PREFIX}lid-x:fp-abc")

    @pytest.mark.asyncio
    async def test_is_valid_false_when_key_absent(self):
        mock_redis = MagicMock()
        mock_redis.exists = AsyncMock(return_value=0)

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            assert await is_fingerprint_valid("lid-x", "fp-stale") is False

    @pytest.mark.asyncio
    async def test_concurrent_renewals_both_remain_valid(self):
        """Two pods race on /renew → both fingerprints are independently registered.

        Whichever cert wins the K8s Secret CAS becomes the active one in use,
        but the loser's cert MUST also auth successfully — otherwise a pod
        that adopted the winner's cert would 401 and the listener fleet would
        lock itself out for the rest of the cert lifetime.
        """
        registered = set()

        mock_redis = MagicMock()

        async def fake_setex(key, ttl, value):
            registered.add(key)

        async def fake_exists(key):
            return 1 if key in registered else 0

        mock_redis.setex = fake_setex
        mock_redis.exists = fake_exists

        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            # Two concurrent renewals → two distinct fingerprints registered.
            await _register_fingerprint("lid-x", "fp-pod-a", 600)
            await _register_fingerprint("lid-x", "fp-pod-b", 600)

            # Both must auth successfully regardless of which won the K8s
            # Secret CAS on the listener side.
            assert await is_fingerprint_valid("lid-x", "fp-pod-a") is True
            assert await is_fingerprint_valid("lid-x", "fp-pod-b") is True

        # And a rogue cert never registered must still be rejected.
        with patch(
            "terrapod.services.agent_pool_service.get_redis_client",
            return_value=mock_redis,
        ):
            assert await is_fingerprint_valid("lid-x", "fp-not-issued") is False

    def test_ttl_buffer_against_clock_skew(self):
        """The fingerprint key TTL is cert lifetime + 60s buffer.

        The buffer prevents the rare case where Redis evicts the fingerprint
        key a moment before the cert's not-after, causing a still-valid cert
        to fail auth. 60s is enough for any realistic API↔Redis clock skew.
        """
        cert = MagicMock()
        cert.not_valid_after_utc = datetime.now(UTC) + timedelta(seconds=600)
        ttl = _fingerprint_ttl(cert)
        # Should be ~660s (600 + 60 buffer); allow slack for test wall-clock drift.
        assert 650 <= ttl <= 670

    def test_ttl_floor_at_60_for_already_expired(self):
        """An expired cert still gets a 60s floor so we don't pass 0/negative TTL.

        Redis SETEX rejects a 0 or negative TTL with a runtime error. Better
        to register the fingerprint briefly (it'll be rejected by the
        not-after check in dependencies.py anyway) than crash the renewal.
        """
        cert = MagicMock()
        cert.not_valid_after_utc = datetime.now(UTC) - timedelta(seconds=600)
        assert _fingerprint_ttl(cert) >= 60
