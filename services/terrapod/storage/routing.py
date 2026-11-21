"""Routes each workspace's objects to the storage location it lives in."""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Protocol

from terrapod.config import DEFAULT_STORAGE_LOCATION
from terrapod.storage.keys import prefix_may_hold_workspace_keys, workspace_id_of_key
from terrapod.storage.protocol import (
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    PresignedURL,
)


class UnknownWorkspaceLocationError(ObjectStoreError):
    """A write for a workspace whose location cannot be resolved (no row yet, or deleted)."""


class LocationResolver(Protocol):
    async def location_for(self, workspace_id: str) -> str | None:
        """The workspace's location name, or None if the workspace is unknown."""
        ...


def remember_location(store: ObjectStore, workspace_id: str, location: str | None) -> bool:
    """Teach a routing store where a workspace's objects live; False if it cannot be taught.

    `LocationResolver` declares only `location_for`. Of the three resolvers only
    the database-backed one can be taught, and `FanOutResolver` answers None for
    every workspace -- so it reaches both callers of this on every object. Hence
    a probe rather than an assumption: calling `remember` on it unguarded turns a
    handled miss into an AttributeError.

    Both callers are paths where the workspace ROW is gone and its delete marker
    is the only remaining authority on where the objects are (#2621): a follower
    copying objects for a workspace deleted before its row replicated, and the
    reaper deleting them. Without it the reaper re-queries per object -- negative
    lookups are deliberately never cached -- and fans every delete across every
    configured location.
    """
    remember = getattr(getattr(store, "resolver", None), "remember", None)
    if remember is None:
        return False
    if location is not None and location not in getattr(store, "locations", {}):
        # A marker naming a location this node no longer configures. Teaching it
        # would make every one of that workspace's objects resolve to an
        # ObjectStoreError; answering False sends the caller back to the
        # slow-but-correct path it already handles.
        return False
    remember(workspace_id, location)
    return True


class StaticDefaultResolver:
    """Places every workspace on the default location."""

    async def location_for(self, workspace_id: str) -> str | None:
        return DEFAULT_STORAGE_LOCATION


class FanOutResolver:
    """Knows no workspace, so reads fan out and writes are refused; for tools without a database."""

    async def location_for(self, workspace_id: str) -> str | None:
        return None


class RoutingStore:
    """An ObjectStore over a default store and named location stores."""

    def __init__(
        self,
        default: ObjectStore,
        locations: dict[str, ObjectStore],
        resolver: LocationResolver,
    ) -> None:
        self.default = default
        self.locations = locations
        self.resolver = resolver

    def _all(self) -> list[ObjectStore]:
        """Every distinct store, default first."""
        stores: list[ObjectStore] = []
        for store in (self.default, *self.locations.values()):
            if not any(s is store for s in stores):
                stores.append(store)
        return stores

    async def _owner(self, key: str) -> ObjectStore | None:
        """The store a key or workspace prefix belongs to; None for an unknown workspace."""
        workspace_id = workspace_id_of_key(key)
        if workspace_id is None:
            return self.default
        name = await self.resolver.location_for(workspace_id)
        if name is None:
            return None
        if name == DEFAULT_STORAGE_LOCATION:
            return self.default
        if name not in self.locations:
            raise ObjectStoreError(
                f"workspace {workspace_id} is on storage location {name!r}, which is not configured"
            )
        return self.locations[name]

    async def _writer(self, key: str) -> ObjectStore:
        store = await self._owner(key)
        if store is None:
            raise UnknownWorkspaceLocationError(
                f"refusing to write {key}: its workspace has no known location"
            )
        return store

    async def _holder(self, key: str) -> ObjectStore | None:
        """The first store holding the key."""
        for store in self._all():
            if await store.exists(key):
                return store
        return None

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        return await (await self._writer(key)).put(key, data, content_type, metadata)

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        return await (await self._writer(key)).put_stream(key, chunks, content_type, metadata)

    async def get(self, key: str) -> bytes:
        store = await self._owner(key)
        if store is not None:
            return await store.get(key)
        for candidate in self._all():
            try:
                return await candidate.get(key)
            except ObjectNotFoundError:
                continue
        raise ObjectNotFoundError(key)

    async def get_stream(
        self,
        key: str,
        chunk_size: int = 256 * 1024,
    ) -> AsyncIterator[bytes]:
        store = await self._owner(key)
        candidates = [store] if store is not None else self._all()
        for candidate in candidates:
            async with aclosing(candidate.get_stream(key, chunk_size)) as stream:
                try:
                    first = await anext(stream)
                except ObjectNotFoundError:
                    if store is not None:
                        raise
                    continue
                except StopAsyncIteration:
                    return
                yield first
                async for chunk in stream:
                    yield chunk
                return
        raise ObjectNotFoundError(key)

    async def delete(self, key: str) -> None:
        store = await self._owner(key)
        if store is not None:
            await store.delete(key)
            return
        for candidate in self._all():
            await candidate.delete(key)

    async def exists(self, key: str) -> bool:
        store = await self._owner(key)
        if store is not None:
            return await store.exists(key)
        return await self._holder(key) is not None

    async def head(self, key: str) -> ObjectMeta:
        store = await self._owner(key)
        if store is not None:
            return await store.head(key)
        for candidate in self._all():
            try:
                return await candidate.head(key)
            except ObjectNotFoundError:
                continue
        raise ObjectNotFoundError(key)

    async def list_prefix(
        self,
        prefix: str,
        *,
        after: str = "",
        limit: int | None = None,
    ) -> list[ObjectMeta]:
        if not prefix_may_hold_workspace_keys(prefix):
            return await self.default.list_prefix(prefix, after=after, limit=limit)
        # Only a prefix holding a whole workspace segment (`state/<ws>/`) names one workspace.
        store = await self._owner(prefix) if workspace_id_of_key(prefix) else None
        if store is not None:
            return await store.list_prefix(prefix, after=after, limit=limit)
        pages = await asyncio.gather(
            *(s.list_prefix(prefix, after=after, limit=limit) for s in self._all())
        )
        merged = heapq.merge(*pages, key=lambda m: m.key)
        return list(itertools.islice(merged, limit))

    async def presigned_get_url(
        self,
        key: str,
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        store = await self._owner(key)
        if store is None:
            store = await self._holder(key) or self.default
        return await store.presigned_get_url(key, expiry_seconds)

    async def presigned_put_url(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        expiry_seconds: int | None = None,
    ) -> PresignedURL:
        return await (await self._writer(key)).presigned_put_url(key, content_type, expiry_seconds)

    async def close(self) -> None:
        results = await asyncio.gather(*(s.close() for s in self._all()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
