"""Refreshing a mirrored tag before serving it (#1425).

A tag is the only mutable thing Terrapod caches. Every other artifact — provider
binaries, CLI binaries, wheels, npm tarballs, blobs addressed by digest — is
pinned to a coordinate that cannot change upstream, which is why none of them
revalidates and why this must not be generalised back to them.

Before this, the first pull of a mirrored tag fixed what it meant here for good:
upstream could move `latest` any number of times and every later pull served the
original digest, silently.

The branch that matters most is the failure one. A pull-through cache exists to
keep answering when upstream cannot be reached, so every way a check can fail
must still return the cached image.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers.oci import _revalidate_tag

CACHED_DIGEST = "sha256:" + "a" * 64
MOVED_DIGEST = "sha256:" + "b" * 64


def _repository():
    repo = MagicMock()
    repo.id = uuid.uuid4()
    repo.name = "quay.io/ansible/awx-ee"
    repo.upstream = "quay.io"
    return repo


def _manifest(digest=CACHED_DIGEST):
    manifest = MagicMock()
    manifest.digest = digest
    return manifest


def _tag_row(checked_hours_ago: float | None):
    """A tag last confirmed against upstream this long ago.

    Expressed in hours because the window is a day: a value in minutes reads as
    "stale" while being comfortably inside the TTL, which is exactly how the
    first version of these tests managed to assert nothing.
    """
    row = MagicMock()
    row.revalidated_at = (
        None
        if checked_hours_ago is None
        else datetime.now(UTC) - timedelta(hours=checked_hours_ago)
    )
    return row


@pytest.fixture
def _upstream():
    with (
        patch("terrapod.services.oci.registry_service.get_tag", new=AsyncMock()) as get_tag,
        patch(
            "terrapod.services.oci.pullthrough_service.current_upstream_digest", new=AsyncMock()
        ) as head,
        patch(
            "terrapod.services.oci.pullthrough_service.resolve_upstream",
            return_value=("quay.io", "ansible/awx-ee"),
        ),
        patch("terrapod.services.oci.pullthrough_service.mirroring_allowed", return_value=True),
    ):
        yield get_tag, head


class TestWithinTheTTL:
    async def test_a_tag_checked_inside_the_ttl_does_not_ask_upstream(self, _upstream) -> None:
        """Otherwise every pull pays a round trip for content we already hold."""
        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=6)
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", cached)

        assert result is cached
        head.assert_not_awaited()


class TestPastTheTTL:
    async def test_an_unchanged_tag_serves_cache_and_resets_the_clock(self, _upstream) -> None:
        get_tag, head = _upstream
        tag_row = _tag_row(checked_hours_ago=200)
        get_tag.return_value = tag_row
        head.return_value = CACHED_DIGEST
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", cached)

        assert result is cached
        head.assert_awaited_once()
        # Confirmed-unchanged still counts as checked, or every pull rechecks.
        assert tag_row.revalidated_at > datetime.now(UTC) - timedelta(seconds=30)

    @patch("terrapod.api.routers.oci._mirror_manifest", new_callable=AsyncMock)
    async def test_a_moved_tag_is_refreshed_before_returning(self, mirror, _upstream) -> None:
        """The point of the feature: this pull gets the new image, not the next."""
        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=200)
        head.return_value = MOVED_DIGEST
        refreshed = _manifest(MOVED_DIGEST)
        mirror.return_value = refreshed

        result = await _revalidate_tag(
            AsyncMock(), AsyncMock(), _repository(), "latest", _manifest()
        )

        assert result is refreshed
        mirror.assert_awaited_once()


class TestALocallyPushedTag:
    """A mirror repository can hold pushed tags too, and they are not ours to move.

    `quay.io/ansible/awx-ee` mirrors, but nothing stops someone pushing their own
    tag into it. Upstream knows nothing about that tag; asking upstream about it
    can only end one of two ways, and one of them is replacing local content with
    a same-named image from somewhere else.

    Provenance is recorded at write time — a mirrored tag is stamped when fetched,
    a pushed one is not — so this is decided by what the row says rather than by
    guessing from the repository.
    """

    async def test_a_pushed_tag_is_never_asked_about(self, _upstream) -> None:
        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=None)  # never fetched
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "mine", cached)

        assert result is cached
        head.assert_not_awaited()

    @patch("terrapod.api.routers.oci._mirror_manifest", new_callable=AsyncMock)
    async def test_upstream_cannot_replace_it(self, mirror, _upstream) -> None:
        """The failure that matters: upstream has its own `mine`, and wins."""
        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=None)
        head.return_value = MOVED_DIGEST  # upstream has something under that name
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "mine", cached)

        assert result is cached
        mirror.assert_not_awaited()


class TestUpstreamUnavailable:
    """Every failure resolves to serving what we already have.

    A cache that stops answering when it cannot reach the internet has failed at
    the one thing it exists for — and in a restricted network there is no second
    source to fall back to.
    """

    async def test_an_unreachable_upstream_serves_the_stale_image(self, _upstream) -> None:
        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=200)
        head.return_value = None  # could not be reached, or refused
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", cached)

        assert result is cached

    async def test_a_failed_check_still_backs_off(self, _upstream) -> None:
        """Retrying on every pull would add upstream's timeout to each one.

        That turns a reachability problem into a latency problem for content we
        are holding locally, which is the opposite of the point.
        """
        get_tag, head = _upstream
        tag_row = _tag_row(checked_hours_ago=200)
        get_tag.return_value = tag_row
        head.return_value = None

        await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", _manifest())

        assert tag_row.revalidated_at > datetime.now(UTC) - timedelta(seconds=30)

    @patch("terrapod.api.routers.oci._mirror_manifest", new_callable=AsyncMock)
    async def test_a_confirmed_move_that_then_fails_to_fetch_serves_the_stale_image(
        self, mirror, _upstream
    ) -> None:
        """Upstream said it moved and then would not hand it over.

        The cached image is still a working answer, and refusing to serve one
        because a *better* one could not be obtained helps nobody.
        """
        from terrapod.services.oci.errors import UNSUPPORTED, OCIError

        get_tag, head = _upstream
        get_tag.return_value = _tag_row(checked_hours_ago=200)
        head.return_value = MOVED_DIGEST
        mirror.side_effect = OCIError(UNSUPPORTED, message="upstream went away")
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", cached)

        assert result is cached


class TestSealed:
    @patch("terrapod.api.routers.oci.settings")
    async def test_a_sealed_node_never_asks_upstream(self, mock_settings, _upstream) -> None:
        """Reaching upstream is exactly what sealing forbids."""
        get_tag, head = _upstream
        mock_settings.registry.cache_only = True
        mock_settings.registry.oci.mirror_tag_ttl_hours = 168
        cached = _manifest()

        result = await _revalidate_tag(AsyncMock(), AsyncMock(), _repository(), "latest", cached)

        assert result is cached
        head.assert_not_awaited()
        get_tag.assert_not_awaited()


class TestItIsActuallyWiredIn:
    """The tests above prove the function; this proves the pull calls it.

    A revalidation helper nothing invokes is the same bug it was written to fix,
    and it would pass every test above.
    """

    def _app(self):
        from terrapod.api.app import create_application
        from terrapod.api.dependencies import AuthenticatedUser
        from terrapod.db.session import get_db
        from terrapod.services.oci.auth import authenticate_oci
        from terrapod.storage import get_storage

        app = create_application()
        app.dependency_overrides[authenticate_oci] = lambda: AuthenticatedUser(
            email="a@b.c",
            display_name=None,
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        storage = AsyncMock()
        storage.get.return_value = b"{}"
        app.dependency_overrides[get_storage] = lambda: storage
        return app

    @patch("terrapod.api.routers.oci._revalidate_tag", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest", new_callable=AsyncMock)
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_pulling_a_cached_mirrored_tag_revalidates(
        self, get_repo, resolve, caps, revalidate
    ) -> None:
        from httpx import ASGITransport, AsyncClient

        from terrapod.auth import capabilities as cap

        repo = _repository()
        repo.labels = {}
        repo.owner_email = "a@b.c"
        get_repo.return_value = repo
        caps.return_value = frozenset({cap.REGISTRY_READ})
        cached = _manifest()
        cached.size = 2
        cached.media_type = "application/vnd.oci.image.manifest.v1+json"
        cached.storage_key = "k"
        resolve.return_value = cached
        revalidate.return_value = cached

        async with AsyncClient(
            transport=ASGITransport(app=self._app()), base_url="http://test"
        ) as client:
            response = await client.get(
                "/v2/quay.io/ansible/awx-ee/manifests/latest",
                headers={"Authorization": "Bearer t"},
            )

        assert response.status_code == 200
        revalidate.assert_awaited_once()

    @patch("terrapod.api.routers.oci._revalidate_tag", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest", new_callable=AsyncMock)
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_pulling_by_digest_never_revalidates(
        self, get_repo, resolve, caps, revalidate
    ) -> None:
        """A digest is immutable by construction; checking it would be pure cost."""
        from httpx import ASGITransport, AsyncClient

        from terrapod.auth import capabilities as cap

        repo = _repository()
        repo.labels = {}
        repo.owner_email = "a@b.c"
        get_repo.return_value = repo
        caps.return_value = frozenset({cap.REGISTRY_READ})
        cached = _manifest()
        cached.size = 2
        cached.media_type = "application/vnd.oci.image.manifest.v1+json"
        cached.storage_key = "k"
        resolve.return_value = cached

        async with AsyncClient(
            transport=ASGITransport(app=self._app()), base_url="http://test"
        ) as client:
            await client.get(
                f"/v2/quay.io/ansible/awx-ee/manifests/{CACHED_DIGEST}",
                headers={"Authorization": "Bearer t"},
            )

        revalidate.assert_not_awaited()

    @patch("terrapod.api.routers.oci._revalidate_tag", new_callable=AsyncMock)
    @patch("terrapod.api.routers.oci.resolve_registry_capabilities_for")
    @patch("terrapod.services.oci.registry_service.resolve_manifest", new_callable=AsyncMock)
    @patch("terrapod.services.oci.registry_service.get_repository", new_callable=AsyncMock)
    async def test_a_pushed_repository_never_revalidates(
        self, get_repo, resolve, caps, revalidate
    ) -> None:
        """There is no upstream to confirm against, and nothing moves under us."""
        from httpx import ASGITransport, AsyncClient

        from terrapod.auth import capabilities as cap

        repo = _repository()
        repo.upstream = None  # pushed
        repo.labels = {}
        repo.owner_email = "a@b.c"
        get_repo.return_value = repo
        caps.return_value = frozenset({cap.REGISTRY_READ})
        cached = _manifest()
        cached.size = 2
        cached.media_type = "application/vnd.oci.image.manifest.v1+json"
        cached.storage_key = "k"
        resolve.return_value = cached

        async with AsyncClient(
            transport=ASGITransport(app=self._app()), base_url="http://test"
        ) as client:
            await client.get("/v2/team/app/manifests/v1", headers={"Authorization": "Bearer t"})

        revalidate.assert_not_awaited()


class TestProvenanceIsRecorded:
    """The stamp that separates a mirrored tag from a pushed one.

    Everything above turns on it, and it fails silently in the dangerous
    direction: if the mirror path stopped stamping, every mirrored tag would read
    as locally pushed and nothing would ever revalidate again — which is exactly
    the bug this feature exists to fix, restored without a single test going red.
    """

    async def test_a_mirrored_tag_is_stamped(self) -> None:
        from terrapod.services.oci import registry_service

        db = AsyncMock()
        db.add = MagicMock()  # sync on a real Session; AsyncMock leaves a stray coroutine
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        manifest = MagicMock(id=uuid.uuid4())

        tag = await registry_service.set_tag(
            db, _repository(), "latest", manifest, from_upstream=True
        )

        assert tag.revalidated_at is not None
        assert tag.revalidated_at > datetime.now(UTC) - timedelta(seconds=30)

    async def test_a_pushed_tag_is_not_stamped(self) -> None:
        from terrapod.services.oci import registry_service

        db = AsyncMock()
        db.add = MagicMock()  # sync on a real Session; AsyncMock leaves a stray coroutine
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        manifest = MagicMock(id=uuid.uuid4())

        tag = await registry_service.set_tag(db, _repository(), "mine", manifest)

        assert tag.revalidated_at is None

    async def test_the_mirror_path_asks_for_the_stamp(self) -> None:
        """Proves the caller passes it, not merely that the parameter exists."""
        import inspect

        from terrapod.api.routers import oci

        source = inspect.getsource(oci._mirror_manifest)
        assert "from_upstream=True" in source, (
            "the mirror path must record provenance, or every cached tag reads as "
            "locally pushed and revalidation silently stops happening"
        )
