"""The follower's object-store copier (#1159).

Most of these are about the copier being **honest and bounded** rather than about
it copying, because copying is the easy half. Specifically:

- a cycle that stopped at its budget must never be reportable as finished;
- one unreadable object must not abandon the class it was in;
- a short write must not be left where the next cycle's `exists()` will call it
  present, because that corruption outlives every retry;
- irreplaceable classes go first, so a cycle that runs out of budget runs out of
  it having done the part that mattered.

The last one is the reason the ordering is asserted rather than left to the
register's declaration order: the register is right today, and a sort that
quietly stopped honouring it would only show up at a failover.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import HABlobsConfig, settings
from terrapod.services import blob_classes, blob_sync
from terrapod.services.blob_classes import COPY, HISTORY, IRREPLACEABLE, VERIFY, BlobClass


class FakeStore:
    """An object store that starts empty and records what was written."""

    def __init__(self, present: set[str] | None = None):
        self.present = set(present or ())
        self.written: dict[str, int] = {}
        self.deleted: list[str] = []

    async def exists(self, key: str) -> bool:
        return key in self.present

    async def put_stream(self, key, chunks, **kwargs):
        from datetime import UTC, datetime

        from terrapod.storage.protocol import ObjectMeta

        size = 0
        async for chunk in chunks:
            size += len(chunk)
        self.written[key] = size
        self.present.add(key)
        return ObjectMeta(
            key=key,
            size_bytes=size,
            content_type="application/octet-stream",
            etag="e",
            last_modified=datetime.now(UTC),
        )

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.present.discard(key)


def _cfg(**kw) -> HABlobsConfig:
    defaults = {"mode": VERIFY, "max_bytes_per_second": 0, "concurrency": 4}
    defaults.update(kw)
    return HABlobsConfig(**defaults)


class TestTheThrottleIsHonest:
    """`_Throttle` is what makes "bandwidth honesty" (#1114) more than a word."""

    async def test_no_rate_means_no_waiting(self):
        throttle = blob_sync._Throttle(bytes_per_second=0, bytes_per_cycle=0)

        await throttle.account(10_000_000)

        assert throttle.spent == 10_000_000
        assert throttle.cap_reached() is False

    async def test_a_cycle_cap_of_zero_never_trips(self):
        """0 means no cap, not a cap of nothing — getting that backwards would
        stop the copier dead on the default config."""
        throttle = blob_sync._Throttle(bytes_per_second=0, bytes_per_cycle=0)

        await throttle.account(1_000_000_000)

        assert throttle.cap_reached() is False

    async def test_the_cycle_cap_trips_once_spent(self):
        throttle = blob_sync._Throttle(bytes_per_second=0, bytes_per_cycle=1000)

        await throttle.account(400)
        assert throttle.cap_reached() is False

        await throttle.account(700)
        assert throttle.cap_reached() is True

    async def test_running_ahead_of_the_rate_waits(self):
        """The bytes are already spent when this is called, so the only lever is
        to make the *next* object wait — which is what keeps a cycle's average
        under the ceiling the operator set."""
        throttle = blob_sync._Throttle(bytes_per_second=1000, bytes_per_cycle=0)
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("asyncio.sleep", new=fake_sleep):
            await throttle.account(5000)

        assert slept, "spending 5000 bytes at 1000 B/s must cost about 5 seconds"
        assert slept[0] > 4


class TestNothingHappensUnlessAskedTo:
    async def test_no_copy_classes_is_the_ordinary_case(self):
        """The default is `verify` everywhere, so on almost every install this
        does nothing at all — and says so rather than logging a cycle."""
        with patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=VERIFY)):
            result = await blob_sync.run_cycle()

        assert result.skipped_reason == "no classes configured to copy"
        assert result.classes == []

    async def test_no_peer_is_a_skip_not_an_error(self):
        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY)),
            patch.object(blob_sync.settings.ha.peer, "url", ""),
        ):
            result = await blob_sync.run_cycle()

        assert result.skipped_reason == "no peer configured"


class TestIrreplaceableFirst:
    async def test_classes_are_ordered_by_effective_tier(self):
        """A cycle that runs out of budget should have spent it on the classes
        whose loss is permanent. Sorted on the EFFECTIVE tier, so a sealed node's
        caches are promoted with it rather than left until last on the one node
        where they are fatal."""
        order: list[str] = []

        async def record(client, token, base, cls, throttle):
            order.append(cls.name)
            return blob_sync.ClassResult(name=cls.name, tier="x")

        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY)),
            patch.object(blob_sync.settings.ha.peer, "url", "https://peer.example"),
            patch.object(blob_sync, "_peer_token", new=AsyncMock(return_value="tok")),
            patch.object(blob_sync, "_sync_class", new=record),
            patch.object(
                blob_sync.blob_classes,
                "CLASSES",
                (
                    BlobClass(name="logs", tier=HISTORY, prefixes=("logs/",)),
                    BlobClass(name="state", tier=IRREPLACEABLE, prefixes=("state/",)),
                ),
            ),
        ):
            await blob_sync.run_cycle()

        assert order == ["state", "logs"]


class TestCopying:
    def _client(self, pages, content=b"payload"):
        """A peer that serves `pages` from the listing endpoint and `content`
        from every content fetch."""
        client = AsyncMock()

        listing = AsyncMock()
        listing.raise_for_status = lambda: None
        listing.json = lambda: pages.pop(0)

        async def request(*args, **kwargs):
            return listing

        stream = AsyncMock()
        stream.raise_for_status = lambda: None

        async def aiter_bytes():
            yield content

        stream.aiter_bytes = aiter_bytes

        class _Ctx:
            async def __aenter__(self):
                return stream

            async def __aexit__(self, *a):
                return False

        client.stream = lambda *a, **kw: _Ctx()
        return client, request

    async def test_only_absent_objects_are_fetched(self):
        """The diff is the point: an object already here costs one `exists()`,
        not a transfer."""
        store = FakeStore(present={"state/a"})
        pages = [
            {
                "data": [
                    {"attributes": {"key": "state/a", "size-bytes": 7}},
                    {"attributes": {"key": "state/b", "size-bytes": 7}},
                ],
                "meta": {"cursor": "state/b", "complete": True},
            }
        ]
        client, request = self._client(pages)
        cls = BlobClass(name="state", tier=IRREPLACEABLE, prefixes=("state/",))

        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY)),
            patch("terrapod.storage.get_storage", return_value=store),
            patch.object(blob_sync, "arequest_with_retry", new=request),
        ):
            result = await blob_sync._sync_class(
                client, "tok", "https://peer", cls, blob_sync._Throttle(0, 0)
            )

        assert result.skipped_present == 1
        assert result.copied == 1
        assert store.written == {"state/b": 7}

    async def test_a_short_write_is_deleted_rather_than_left(self):
        """The dangerous outcome: a partial object that the next cycle's
        `exists()` calls present, so the corruption outlives every retry.
        Removing it puts the key back in the diff."""
        store = FakeStore()
        pages = [
            {
                "data": [{"attributes": {"key": "state/a", "size-bytes": 999}}],
                "meta": {"cursor": "state/a", "complete": True},
            }
        ]
        client, request = self._client(pages, content=b"short")
        cls = BlobClass(name="state", tier=IRREPLACEABLE, prefixes=("state/",))

        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY)),
            patch("terrapod.storage.get_storage", return_value=store),
            patch.object(blob_sync, "arequest_with_retry", new=request),
        ):
            result = await blob_sync._sync_class(
                client, "tok", "https://peer", cls, blob_sync._Throttle(0, 0)
            )

        assert store.deleted == ["state/a"]
        assert result.failed == 1
        assert result.copied == 0

    async def test_one_bad_object_does_not_abandon_the_class(self):
        """The rest of the estate is still worth copying, and the next cycle
        re-diffs the failure anyway."""
        store = FakeStore()
        pages = [
            {
                "data": [
                    {"attributes": {"key": "state/a", "size-bytes": 7}},
                    {"attributes": {"key": "state/b", "size-bytes": 7}},
                ],
                "meta": {"cursor": "state/b", "complete": True},
            }
        ]
        client, request = self._client(pages)
        cls = BlobClass(name="state", tier=IRREPLACEABLE, prefixes=("state/",))

        calls = {"n": 0}
        real_put = store.put_stream

        async def flaky(key, chunks, **kw):
            calls["n"] += 1
            if key == "state/a":
                raise RuntimeError("unreadable")
            return await real_put(key, chunks, **kw)

        store.put_stream = flaky

        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY, concurrency=1)),
            patch("terrapod.storage.get_storage", return_value=store),
            patch.object(blob_sync, "arequest_with_retry", new=request),
        ):
            result = await blob_sync._sync_class(
                client, "tok", "https://peer", cls, blob_sync._Throttle(0, 0)
            )

        assert result.failed == 1
        assert result.copied == 1
        assert "state/b" in store.written

    async def test_a_cycle_that_hits_the_cap_says_so(self):
        """The honesty property. A big `bytes_copied` with `stopped_early` set is
        progress; the same number without it is completion, and confusing the two
        is how an operator fails over onto a half-copied store."""
        store = FakeStore()
        pages = [
            {
                "data": [{"attributes": {"key": f"state/{i}", "size-bytes": 7}} for i in range(5)],
                "meta": {"cursor": "state/4", "complete": True},
            }
        ]
        client, request = self._client(pages)
        cls = BlobClass(name="state", tier=IRREPLACEABLE, prefixes=("state/",))

        with (
            patch.object(blob_sync.settings.ha, "blobs", _cfg(mode=COPY, concurrency=1)),
            patch("terrapod.storage.get_storage", return_value=store),
            patch.object(blob_sync, "arequest_with_retry", new=request),
        ):
            result = await blob_sync._sync_class(
                client,
                "tok",
                "https://peer",
                cls,
                # One object's worth of budget, five objects to copy.
                blob_sync._Throttle(bytes_per_second=0, bytes_per_cycle=7),
            )

        assert result.stopped_early == "cycle byte cap reached"
        assert result.copied < 5, "the cap must actually stop the copy"

    async def test_stopping_early_is_visible_on_the_whole_cycle(self):
        result = blob_sync.SyncResult(
            classes=[
                blob_sync.ClassResult(name="state", tier=IRREPLACEABLE, copied=40, bytes_copied=1),
                blob_sync.ClassResult(
                    name="registry_modules",
                    tier=IRREPLACEABLE,
                    copied=2,
                    stopped_early="cycle byte cap reached",
                ),
            ]
        )

        assert result.objects_copied == 42
        assert result.stopped_early == ["registry_modules"]


class TestTheFollowerIsAllowedToRunThis:
    def test_blob_sync_is_in_the_scheduler_follower_safe_set(self):
        """A follower that stops copying promotes with rows whose objects are not
        there — the failure that looks like success. Gating it would be
        self-defeating in the same way gating the settings pull would be."""
        from terrapod.services.scheduler import _FOLLOWER_SAFE_TASKS

        assert "blob_sync" in _FOLLOWER_SAFE_TASKS


class TestOwningClassResolvesOverlaps:
    """`state/index.yaml` sits under `state/` and is its own class."""

    def test_the_most_specific_prefix_wins(self):
        assert blob_classes.owning_class("state/index.yaml").name == "state_index"
        assert blob_classes.owning_class("state/ws-1/sv-1.tfstate").name == "state"

    def test_delete_markers_are_their_own_class_not_state(self):
        """`state/deleted/` sits under `state/` too, and the whole reason it is
        a separate class is that `state` is `encrypted_at_rest` — which
        replication declines wholesale when app-layer encryption is on. If
        ownership resolved to `state`, an encrypted deployment's standby would
        carry no undelete index at all: precisely what the flat prefix exists
        to prevent, and invisible until somebody needed to restore (#1297).
        """
        marker = "state/deleted/0192f3a1-0000-7000-8000-00000000000a.json"
        owner = blob_classes.owning_class(marker)
        assert owner is not None
        assert owner.name == "deleted_workspace_markers"
        assert not owner.encrypted_at_rest

        # ...and a real state object under the same root still resolves to
        # `state`, so the narrower prefix has not swallowed its neighbour.
        assert blob_classes.owning_class("state/0192f3a1/sv-1.tfstate").name == "state"

    def test_an_unregistered_key_belongs_to_nothing(self):
        assert blob_classes.owning_class("nowhere/x") is None

    def test_owns_is_exclusive(self):
        state = blob_classes.get("state")
        index = blob_classes.get("state_index")

        assert blob_classes.owns(state, "state/ws-1/sv-1.tfstate") is True
        assert blob_classes.owns(state, "state/index.yaml") is False, (
            "the index is its own class; serving it under `state` would copy and count it twice"
        )
        assert blob_classes.owns(index, "state/index.yaml") is True

    @pytest.mark.parametrize(
        "key",
        [
            "../../etc/passwd",
            # Starts with `state/` and leaves it. A prefix test admits this,
            # which is exactly why the gate is not a prefix test.
            "state/../cache/binaries/tofu",
            "state/ws/../../etc/passwd",
            "/state/ws/sv.tfstate",
            "state//ws/sv.tfstate",
            "state/./ws/sv.tfstate",
            "state\\ws\\sv.tfstate",
            "statex/ws/sv.tfstate",
            "state/ws/",
            "",
        ],
    )
    def test_owns_refuses_anything_outside_the_class(self, key):
        """The gate the content endpoint leans on.

        Whether a backend would resolve a `..` varies — the filesystem store
        would, an object store treats it as a literal key — and "it depends on the
        backend" is not a property to rest a security gate on. An abnormal key
        belongs to no class, which makes the question moot instead.
        """
        assert blob_classes.owns(blob_classes.get("state"), key) is False
        assert blob_classes.owning_class(key) is None or not key.startswith("state/")

    def test_a_normal_key_is_still_accepted(self):
        """The negative cases above must not have been bought by rejecting
        everything."""
        assert blob_classes.is_safe_key("state/ws-1/sv-1.tfstate") is True
        assert blob_classes.owns(blob_classes.get("state"), "state/ws-1/sv-1.tfstate") is True


class TestPerNodeEncryptedClassesAreNotCopied:
    """A byte-for-byte copy of a per-node-encrypted class is worse than none.

    With `encryption.enabled` each node envelopes state with ITS OWN data key,
    and `crypto_keys` is deliberately never replicated. Copying the ciphertext
    puts every object on the peer, so readiness reports a clean bill of health
    and the failure first appears at failover — as an AES-GCM tag error on every
    state file at once, which is total state loss with a green light in front of
    it. The column path is fine (decrypt on read, re-encrypt on write); the blob
    path has no such step, so it declines instead.
    """

    async def test_the_state_class_is_skipped_when_encryption_is_on(self):
        cls = blob_classes.get("state")
        assert cls.encrypted_at_rest, "state is enveloped per node — see #635"

        with patch.object(settings.encryption, "enabled", True):
            result = await blob_sync._sync_class(
                MagicMock(), "tok", "https://peer.test", cls, MagicMock()
            )

        assert result.copied == 0
        assert result.examined == 0
        assert "undecryptable" in (result.stopped_early or "")

    async def test_it_copies_normally_when_encryption_is_off(self):
        """The default. Nothing about this feature changes for the vast majority
        of deployments, which do not turn app-layer encryption on."""
        cls = blob_classes.get("state")

        client_resp = MagicMock()
        client_resp.json.return_value = {"data": [], "meta": {"cursor": "", "complete": True}}
        client_resp.raise_for_status = MagicMock()
        throttle = MagicMock()
        throttle.cap_reached.return_value = False

        with (
            patch.object(settings.encryption, "enabled", False),
            patch("terrapod.storage.get_storage", return_value=AsyncMock()),
            patch("terrapod.services.blob_sync.arequest_with_retry", return_value=client_resp),
        ):
            result = await blob_sync._sync_class(
                AsyncMock(), "tok", "https://peer.test", cls, throttle
            )

        assert result.stopped_early is None or "undecryptable" not in result.stopped_early

    async def test_an_unencrypted_class_is_never_skipped_for_this_reason(self):
        cls = blob_classes.get("registry_modules")
        assert not cls.encrypted_at_rest
