"""The archive listing against a REAL storage backend (#2474).

The service tests drive a double that implements `list_prefix`'s contract as
documented — key order and an exclusive cursor. That is the right shape for
testing the pagination logic and says nothing about whether a backend actually
behaves that way, which is the half this cache depends on: there are no rows
behind an archive, so the walk IS the index, and a backend whose ordering or
cursor differed would hand an operator a page that silently repeated or skipped
entries.

It sits in the STORAGE tier rather than the integration one, which is where the
tier rules put it: it needs a real storage backend and no database at all, so
paying for Postgres would buy nothing — and it runs in the fast unit shard,
where a cursor regression is caught in seconds rather than minutes.

So this drives the filesystem backend for real. It is the backend a dev stack
and the eval profile use, and the one whose `list_prefix` is hand-written rather
than delegated to a cloud SDK's own pagination — so it is the one where "sorted
on the KEY rather than the Path so the order matches what the object-store
backends produce" is a claim in a comment rather than someone else's guarantee.
"""

from __future__ import annotations

import pathlib

import pytest

from terrapod.services import cache_inventory as ci
from terrapod.storage.filesystem import FilesystemStore


@pytest.fixture
def store(tmp_path: pathlib.Path) -> FilesystemStore:
    return FilesystemStore(root_dir=str(tmp_path), hmac_secret="test")


async def _put(store: FilesystemStore, key: str, size: int) -> None:
    await store.put(key, b"x" * size, content_type="application/gzip")


async def _seed(store: FilesystemStore, specs: list[tuple[str, int]]) -> None:
    for key, size in specs:
        await _put(store, key, size)


class TestTheWalkAgainstRealStorage:
    async def test_keys_come_back_in_lexicographic_order(self, store):
        """The ordering the cursor depends on.

        Seeded out of order on purpose: a backend returning filesystem order
        would pass a test seeded in order and break the moment the directory
        was written in a different sequence.
        """
        await _seed(
            store,
            [
                ("vcs_archives/c/o/r/zz-full.tar.gz", 10),
                ("vcs_archives/c/o/r/aa-full.tar.gz", 10),
                ("vcs_archives/c/o/r/mm-full.tar.gz", 10),
            ],
        )
        entries, _ = await ci.list_archives(store, limit=10)
        keys = [e.key for e in entries]
        assert keys == sorted(keys), f"the backend did not return key order: {keys}"

    async def test_the_cursor_walks_the_whole_estate_exactly_once(self, store):
        """The property an operator paging a large cache relies on.

        Asserted as a full walk rather than two pages, because the failure this
        catches is cumulative: an off-by-one in the cursor shows up as one
        repeated or one skipped key per page, which two pages can miss and a
        twelve-page walk cannot.
        """
        await _seed(store, [(f"vcs_archives/c/o/r/s{i:03d}-full.tar.gz", 5) for i in range(25)])

        seen: list[str] = []
        cursor = ""
        for _ in range(20):  # bounded, so a cursor that never advances fails here
            page, cursor = await ci.list_archives(store, after=cursor, limit=4)
            seen.extend(e.key for e in page)
            if not cursor:
                break
        else:
            pytest.fail("the cursor never emptied; the walk does not terminate")

        assert len(seen) == 25, f"walked {len(seen)} of 25"
        assert len(set(seen)) == 25, "an entry was returned twice"
        assert seen == sorted(seen), "the walk left key order"

    async def test_the_size_comes_from_the_object_not_the_key(self, store):
        """`ObjectMeta.size_bytes`, which this module first read as `size` --
        inferred from the dataclass I had written rather than off the protocol.
        Every byte figure would have been an AttributeError at the first
        request, and no amount of doubling would have caught it: a double is
        spelled however its author spells it."""
        await _seed(store, [("vcs_archives/c/o/r/a-full.tar.gz", 4096)])
        entries, _ = await ci.list_archives(store, limit=10)
        assert entries[0].size == 4096

    async def test_last_modified_is_reported_as_rfc3339_with_a_Z(self, store):
        """The house timestamp format, and the one a client parses."""
        await _seed(store, [("vcs_archives/c/o/r/a-full.tar.gz", 1)])
        entries, _ = await ci.list_archives(store, limit=10)
        assert entries[0].last_modified.endswith("Z"), entries[0].last_modified

    async def test_narrowing_by_connection_does_not_reach_another_connection(self, store):
        """Scoping by connection is a cache-isolation property, not just a
        filter: two VCS connections pointing at the same repository must not see
        each other's entries, which is why the key is scoped by connection in
        the first place."""
        await _seed(
            store,
            [
                ("vcs_archives/conn-a/acme/infra/a-full.tar.gz", 1),
                ("vcs_archives/conn-b/acme/infra/a-full.tar.gz", 1),
            ],
        )
        entries, _ = await ci.list_archives(store, connection_id="conn-a", limit=10)
        assert [e.connection_id for e in entries] == ["conn-a"]

    async def test_the_walk_ignores_everything_outside_the_archive_prefix(self, store):
        """A shared bucket holds state versions and plan artifacts too, and the
        listing must not report them as cached archives."""
        await _seed(
            store,
            [
                ("vcs_archives/c/o/r/a-full.tar.gz", 1),
                ("state_versions/ws-1/4.tfstate", 1),
                ("plan_artifacts/run-1/plan.json", 1),
            ],
        )
        entries, _ = await ci.list_archives(store, limit=10)
        assert [e.key for e in entries] == ["vcs_archives/c/o/r/a-full.tar.gz"]


class TestTotalsAndEvictionAgainstRealStorage:
    async def test_the_totals_group_by_repository(self, store):
        await _seed(
            store,
            [
                ("vcs_archives/c/acme/big/a-full.tar.gz", 300),
                ("vcs_archives/c/acme/big/b-full.tar.gz", 200),
                ("vcs_archives/c/acme/small/a-full.tar.gz", 10),
            ],
        )
        totals, truncated = await ci.archive_totals(store)
        assert truncated is False
        assert [(t.repo, t.archives, t.bytes) for t in totals] == [
            ("big", 2, 500),
            ("small", 1, 10),
        ]

    async def test_an_eviction_actually_removes_the_objects(self, store):
        """Driven against real storage because the point of an eviction is that
        the bytes go: a double's `delete` records a call, and a backend that
        accepted the call and kept the file would look identical."""
        await _seed(store, [(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 100) for i in range(3)])
        result = await ci.evict_archives(store)
        assert (result.files, result.bytes) == (3, 300)

        remaining, _ = await ci.list_archives(store, limit=10)
        assert remaining == [], "the eviction reported bytes it did not remove"

    async def test_a_refused_key_leaves_the_object_in_place(self, store):
        """The guard's whole purpose, against real storage: a state version must
        still be there afterwards."""
        await _seed(store, [("state_versions/ws-1/4.tfstate", 50)])
        assert await ci.delete_archive(store, "state_versions/ws-1/4.tfstate") is False
        assert await store.exists("state_versions/ws-1/4.tfstate") is True

    async def test_a_BOUNDED_sweep_resumes_against_REAL_storage_until_the_cache_is_empty(
        self, store, monkeypatch
    ):
        """#2491 against a real backend, which is the only place the claim can
        be made.

        The service tests drive a double that implements `list_prefix`'s cursor
        as documented, so they prove the eviction asks the right question. They
        say nothing about whether a backend's cursor actually resumes where the
        previous page stopped -- and the whole fix rests on that, because there
        are no rows behind an archive and the walk IS the index. The filesystem
        backend's `after` handling is hand-written rather than delegated to a
        cloud SDK's pagination, so this is where it is a claim rather than
        somebody else's guarantee.

        The loop is the operator's own procedure: call, and while `truncated`,
        call again with the cursor. It is bounded by an assertion rather than by
        hope -- a sweep that fails to advance would otherwise hang the suite,
        which is exactly what the pre-fix code did.
        """
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 2)
        await _seed(store, [(f"vcs_archives/c/o/r/k{i:02d}-full.tar.gz", 10) for i in range(7)])

        after, calls, removed, seen = "", 0, 0, []
        while True:
            calls += 1
            assert calls < 20, "the sweep did not terminate"
            result = await ci.evict_archives(store, after=after)
            removed += result.files
            if not result.truncated:
                break
            assert result.cursor, "truncated with no cursor: nothing can resume it"
            assert result.cursor > after, (
                f"the cursor did not advance ({result.cursor!r} after {after!r}), "
                "so the window restarts and the rest of the estate is unreachable"
            )
            seen.append(result.cursor)
            after = result.cursor

        assert removed == 7, f"the sweep removed {removed} of 7 in {calls} calls"
        remaining, _ = await ci.list_archives(store, limit=100)
        assert remaining == [], "the sweep reported a complete purge and left objects"
        # Four calls for seven keys at two per window: three full windows plus
        # the short one that ends it. Pinned so a bound that silently stopped
        # being applied shows up as a count rather than as a slow test.
        assert calls == 4, f"{calls} calls for 7 keys at 2 per window"

    async def test_a_sweep_that_FITS_in_one_window_reports_nothing_left(self, store):
        """The control. Every assertion above about truncation being reported is
        worth nothing unless the same code reports FALSE when the sweep really
        did finish -- a `truncated = True` constant would satisfy all of them."""
        await _seed(store, [(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 10) for i in range(3)])
        result = await ci.evict_archives(store)
        assert (result.files, result.truncated, result.cursor) == (3, False, "")
