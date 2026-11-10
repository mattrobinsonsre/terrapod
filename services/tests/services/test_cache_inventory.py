"""Enumerating and evicting the package cache and the VCS archive cache (#2474).

The two are enumerated by different means and the tests follow that split: the
package cache has a row per artifact, so it is a database query; the archive
cache has no rows at all, so it is a storage walk whose page comes from a key
cursor.

The key parser gets a table of its own because it is the only thing standing
between an operator and an unreadable estate -- an archive is addressed entirely
by its key, so a parser that drops what it cannot read would hide exactly the
entries nobody else will ever show them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services import cache_inventory as ci


def _now() -> datetime:
    return datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@dataclass
class _Obj:
    """An `ObjectMeta` stand-in.

    Spelled `size_bytes`, which is the protocol's own field name -- this module
    first read it as `size`, inferred from the dataclass I had written rather
    than off `storage/protocol.py`, and every byte figure would have been an
    AttributeError at the first request.
    """

    key: str
    size_bytes: int = 0
    last_modified: datetime | None = None


class _Storage:
    """A storage double that honours `list_prefix`'s ORDERING and CURSOR.

    Both are part of that method's contract, and a double that ignored them
    would make the cursor tests vacuous: a page is only meaningful because the
    backend returns keys in lexicographic order and `after` is exclusive.
    """

    def __init__(self, objects: list[_Obj]):
        self.objects = sorted(objects, key=lambda o: o.key)
        self.deleted: list[str] = []

    async def list_prefix(self, prefix: str, *, after: str = "", limit: int | None = None):
        out = [o for o in self.objects if o.key.startswith(prefix) and o.key > after]
        return out if limit is None else out[:limit]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects = [o for o in self.objects if o.key != key]


class TestTheArchiveKeyParser:
    @pytest.mark.parametrize(
        "key,expected",
        [
            (
                "vcs_archives/conn1/acme/infra/abc123-full.tar.gz",
                ("conn1", "acme", "infra", "abc123", "full"),
            ),
            (
                "vcs_archives/conn1/acme/infra/abc123-9f8e7d.tar.gz",
                ("conn1", "acme", "infra", "abc123", "9f8e7d"),
            ),
        ],
    )
    def test_a_well_formed_key_reads_back_into_its_parts(self, key, expected):
        e = ci.parse_archive_key(key)
        assert e is not None
        assert (e.connection_id, e.owner, e.repo, e.sha, e.paths_hash) == expected

    def test_it_splits_on_the_FIRST_hyphen(self):
        """A commit sha is hex and carries no hyphen; a paths hash may. Splitting
        on the last one would move half the sha into the paths hash."""
        e = ci.parse_archive_key("vcs_archives/c/o/r/deadbeef-a-b-c.tar.gz")
        assert e is not None
        assert e.sha == "deadbeef"
        assert e.paths_hash == "a-b-c"

    @pytest.mark.parametrize(
        "key",
        [
            "plan_artifacts/run-1/plan.tar.gz",  # another prefix entirely
            "vcs_archives/too/few.tar.gz",  # not four segments
            "vcs_archives/c/o/r/t/o/o/deep-full.tar.gz",  # too many
            "vcs_archives/c/o/r/noseparator.tar.gz",  # no hyphen
            "vcs_archives/c/o/r/-full.tar.gz",  # empty sha
            "vcs_archives/c/o/r/abc-.tar.gz",  # empty paths hash
            "vcs_archives/c/o/r/abc-full.tgz",  # wrong extension
        ],
    )
    def test_a_key_that_does_not_fit_returns_None(self, key):
        assert ci.parse_archive_key(key) is None


class TestTheArchiveListing:
    @pytest.mark.asyncio
    async def test_a_page_stops_at_the_limit_and_hands_back_a_cursor(self):
        objs = [_Obj(f"vcs_archives/c/o/r/sha{i:02d}-full.tar.gz", 100) for i in range(10)]
        entries, cursor = await ci.list_archives(_Storage(objs), limit=4)
        assert len(entries) == 4
        assert cursor == entries[-1].key, "the cursor must be this page's last key"

    @pytest.mark.asyncio
    async def test_the_cursor_resumes_without_repeating_an_entry(self):
        objs = [_Obj(f"vcs_archives/c/o/r/sha{i:02d}-full.tar.gz", 100) for i in range(10)]
        storage = _Storage(objs)
        first, cursor = await ci.list_archives(storage, limit=4)
        second, _ = await ci.list_archives(storage, after=cursor, limit=4)
        keys = [e.key for e in first] + [e.key for e in second]
        assert len(keys) == len(set(keys)), "the cursor is exclusive; nothing repeats"
        assert keys == sorted(keys), "a resumed walk stays in key order"

    @pytest.mark.asyncio
    async def test_a_SHORT_page_hands_back_an_EMPTY_cursor(self):
        """How a caller knows it has finished.

        A cursor pointing at the last key of a short page would make the caller
        ask again, get nothing, and have to infer the end from an empty list --
        one extra round trip per walk and an ambiguity in the middle of it.
        """
        objs = [_Obj(f"vcs_archives/c/o/r/sha{i}-full.tar.gz", 1) for i in range(3)]
        entries, cursor = await ci.list_archives(_Storage(objs), limit=10)
        assert len(entries) == 3
        assert cursor == ""

    @pytest.mark.asyncio
    async def test_narrowing_by_connection_narrows_the_PREFIX(self):
        """Not a filter over a page: an operator inspecting one connection must
        not pay to walk the whole estate, and a page filtered after the fact
        would come back short or empty while entries existed."""
        objs = [
            _Obj("vcs_archives/c1/o/r/a-full.tar.gz", 1),
            _Obj("vcs_archives/c2/o/r/b-full.tar.gz", 1),
            _Obj("vcs_archives/c2/o/r/c-full.tar.gz", 1),
        ]
        entries, _ = await ci.list_archives(_Storage(objs), connection_id="c2", limit=10)
        assert {e.key for e in entries} == {
            "vcs_archives/c2/o/r/b-full.tar.gz",
            "vcs_archives/c2/o/r/c-full.tar.gz",
        }

    @pytest.mark.asyncio
    async def test_an_unparseable_key_is_SURFACED_not_skipped(self):
        """The entry an operator most needs to see.

        There is no row behind an archive, so a key this module cannot read is
        one nothing else will ever show them -- and it is still occupying the
        bucket. Dropping it would make the listing agree with itself and lie
        about the estate.
        """
        objs = [
            _Obj("vcs_archives/c/o/r/good-full.tar.gz", 10),
            _Obj("vcs_archives/mangled.tar.gz", 999),
        ]
        entries, _ = await ci.list_archives(_Storage(objs), limit=10)
        by_key = {e.key: e for e in entries}
        assert "vcs_archives/mangled.tar.gz" in by_key, "an unreadable key was dropped"
        odd = by_key["vcs_archives/mangled.tar.gz"]
        assert odd.size == 999, "the size must still be reported for an unreadable key"
        assert odd.owner == "" and odd.repo == "", (
            "the parts it could not resolve must be empty rather than invented"
        )

    @pytest.mark.asyncio
    async def test_the_limit_is_bounded_by_the_scan_ceiling(self):
        objs = [_Obj(f"vcs_archives/c/o/r/s{i:05d}-full.tar.gz", 1) for i in range(20)]
        entries, _ = await ci.list_archives(_Storage(objs), limit=10**9)
        assert len(entries) == 20  # everything there is, but the ask was clamped

    @pytest.mark.asyncio
    async def test_a_zero_or_negative_limit_still_returns_a_page(self):
        """A caller sending 0 means "a page", not "nothing": returning an empty
        list would read as an empty cache."""
        objs = [_Obj("vcs_archives/c/o/r/a-full.tar.gz", 1)]
        for limit in (0, -5):
            entries, _ = await ci.list_archives(_Storage(objs), limit=limit)
            assert len(entries) == 1


class TestTheArchiveTotals:
    @pytest.mark.asyncio
    async def test_bytes_and_counts_group_by_repository_largest_first(self):
        objs = [
            _Obj("vcs_archives/c/acme/small/a-full.tar.gz", 10),
            _Obj("vcs_archives/c/acme/big/a-full.tar.gz", 500),
            _Obj("vcs_archives/c/acme/big/b-full.tar.gz", 400),
        ]
        totals, truncated = await ci.archive_totals(_Storage(objs))
        assert [(t.repo, t.archives, t.bytes) for t in totals] == [
            ("big", 2, 900),
            ("small", 1, 10),
        ]
        assert truncated is False

    @pytest.mark.asyncio
    async def test_truncated_is_TRUE_when_the_walk_hits_its_ceiling(self, monkeypatch):
        """The honest half, and the reason it exists.

        There is nothing to aggregate in the database, so this walks storage and
        the walk has to stop somewhere. A total that silently omitted the rest
        would understate exactly what an operator came here to find -- a figure
        smaller than the problem, with nothing saying so.
        """
        monkeypatch.setattr(ci, "MAX_ARCHIVE_SCAN", 3)
        objs = [_Obj(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 1) for i in range(5)]
        totals, truncated = await ci.archive_totals(_Storage(objs))
        assert truncated is True
        assert sum(t.archives for t in totals) == 3

    @pytest.mark.asyncio
    async def test_an_unparseable_key_is_left_out_of_the_TOTALS(self):
        """The opposite choice from the listing, deliberately: a total is
        per-repository and an unreadable key names no repository, so there is no
        bucket to put it in. The listing is where it surfaces."""
        objs = [
            _Obj("vcs_archives/c/o/r/a-full.tar.gz", 10),
            _Obj("vcs_archives/mangled.tar.gz", 999),
        ]
        totals, _ = await ci.archive_totals(_Storage(objs))
        assert sum(t.bytes for t in totals) == 10


class TestDeletingOneArchive:
    @pytest.mark.asyncio
    async def test_it_deletes_the_key(self):
        storage = _Storage([_Obj("vcs_archives/c/o/r/a-full.tar.gz", 1)])
        assert await ci.delete_archive(storage, "vcs_archives/c/o/r/a-full.tar.gz") is True
        assert storage.deleted == ["vcs_archives/c/o/r/a-full.tar.gz"]

    @pytest.mark.parametrize(
        "key",
        [
            "state_versions/ws-1/4.tfstate",
            "plan_artifacts/run-1/plan.json",
            "provider_cache/registry.terraform.io/hashicorp/aws/5.0.0/x.zip",
            "vcs_archives/../state_versions/ws-1/4.tfstate",
            "",
            "/etc/passwd",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_key_outside_the_archive_prefix_is_REFUSED(self, key):
        """This endpoint takes a storage key from a request.

        Without the check an admin typo -- or anything that reached an admin
        token -- could delete a state version, a plan artifact or a provider
        binary through a route whose name says "cache". Nothing is deleted on a
        refusal, which is the assertion that matters: a guard that refused and
        deleted anyway would read as working.
        """
        storage = _Storage([])
        assert await ci.delete_archive(storage, key) is False
        assert storage.deleted == [], "a refused key still reached storage.delete"


class TestEvictingArchivesInBulk:
    @pytest.mark.asyncio
    async def test_everything_when_no_filter_is_given(self):
        objs = [_Obj(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 100) for i in range(3)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage)
        assert (result.files, result.bytes) == (3, 300)
        assert len(storage.deleted) == 3

    @pytest.mark.asyncio
    async def test_narrowed_to_one_connection(self):
        objs = [
            _Obj("vcs_archives/c1/o/r/a-full.tar.gz", 10),
            _Obj("vcs_archives/c2/o/r/b-full.tar.gz", 20),
        ]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage, connection_id="c1")
        assert (result.files, result.bytes) == (1, 10)
        assert storage.deleted == ["vcs_archives/c1/o/r/a-full.tar.gz"]

    @pytest.mark.asyncio
    async def test_age_is_the_objects_WRITE_time(self):
        """Not an access time, because an archive has none: it is immutable once
        written, so its write time IS its age. That makes this blunter than the
        package eviction and the docs say so -- an archive a run pulled this
        morning is evicted if its commit is old enough."""
        old = _Obj("vcs_archives/c/o/r/old-full.tar.gz", 10, _now() - timedelta(days=30))
        new = _Obj("vcs_archives/c/o/r/new-full.tar.gz", 20, _now() - timedelta(hours=1))
        storage = _Storage([old, new])
        result = await ci.evict_archives(storage, older_than_days=7)
        assert (result.files, result.bytes) == (1, 10)
        assert storage.deleted == ["vcs_archives/c/o/r/old-full.tar.gz"]

    @pytest.mark.asyncio
    async def test_an_object_with_no_last_modified_is_KEPT_by_an_age_filter(self):
        """Fail-closed on a missing timestamp. An age filter decides what to
        DELETE, so an unknown age has to mean "leave it" -- treating it as
        infinitely old would make a backend that reports no timestamp empty the
        cache."""
        storage = _Storage([_Obj("vcs_archives/c/o/r/a-full.tar.gz", 10, None)])
        result = await ci.evict_archives(storage, older_than_days=7)
        assert (result.files, result.bytes) == (0, 0)
        assert storage.deleted == []


# ── the package cache ──────────────────────────────────────────────────────


def _file(**kw):
    """A `CachedPackageFile` stand-in with every attribute the callers read."""
    row = MagicMock()
    row.id = kw.get("id", "f1")
    row.ecosystem = kw.get("ecosystem", "pypi")
    row.name = kw.get("name", "flask")
    row.version = kw.get("version", "3.0.0")
    row.filename = kw.get("filename", "flask-3.0.0-py3-none-any.whl")
    row.storage_key = kw.get("storage_key", "package_cache/pypi/flask/x.whl")
    row.size = kw.get("size", 1000)
    row.digest = kw.get("digest", "sha256:abc")
    row.cached_at = kw.get("cached_at", _now())
    row.last_accessed_at = kw.get("last_accessed_at", _now())
    return row


def _db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    return db


class TestDeletingOnePackageFile:
    @pytest.mark.asyncio
    async def test_the_OBJECT_goes_before_the_ROW(self):
        """The other order can leave an object with no row pointing at it:
        invisible to this page, still paying for storage, and re-cached under a
        NEW row on the next request rather than reusing it. A row whose object is
        already gone is the recoverable direction -- the next fetch repopulates
        it."""
        row = _file()
        db = _db([])
        db.get = AsyncMock(return_value=row)
        order: list[str] = []
        storage = MagicMock()
        storage.delete = AsyncMock(side_effect=lambda k: order.append(f"object:{k}"))
        db.delete = AsyncMock(side_effect=lambda r: order.append("row"))

        assert await ci.delete_package_file(db, storage, "f1") is True
        assert order == [f"object:{row.storage_key}", "row"]

    @pytest.mark.asyncio
    async def test_a_missing_row_returns_False_and_deletes_nothing(self):
        db = _db([])
        db.get = AsyncMock(return_value=None)
        storage = MagicMock()
        storage.delete = AsyncMock()
        assert await ci.delete_package_file(db, storage, "nope") is False
        storage.delete.assert_not_awaited()


class TestEvictingPackageFilesInBulk:
    @pytest.mark.asyncio
    async def test_it_reports_the_files_and_bytes_it_removed(self):
        rows = [_file(id="a", size=100), _file(id="b", size=250)]
        db = _db(rows)
        storage = MagicMock()
        storage.delete = AsyncMock()
        result = await ci.evict_package_files(db, storage)
        assert (result.files, result.bytes) == (2, 350)
        assert storage.delete.await_count == 2

    @pytest.mark.asyncio
    async def test_an_empty_match_removes_nothing_and_touches_no_storage(self):
        db = _db([])
        storage = MagicMock()
        storage.delete = AsyncMock()
        result = await ci.evict_package_files(db, storage, ecosystem="npm")
        assert (result.files, result.bytes) == (0, 0)
        storage.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_age_filter_reads_LAST_ACCESS_not_cached_at(self):
        """Access-based, never write-based, for the reason the model records: an
        artifact every run pulls would be evicted merely for being old and
        re-fetched immediately, costing the bandwidth the cache exists to save.

        Asserted on the SQL rather than on a filtered result, because the filter
        is applied in the database -- a double returning rows regardless would
        make a result-level assertion pass whichever column the query names.
        """
        db = _db([])
        storage = MagicMock()
        storage.delete = AsyncMock()
        await ci.evict_package_files(db, storage, older_than_days=30)
        clause = str(db.execute.await_args_list[0].args[0].whereclause)
        assert "last_accessed_at" in clause
        assert "cached_at" not in clause, (
            "the age filter reads cached_at, which evicts a hot artifact for being old"
        )


class TestTheListingQuery:
    @pytest.mark.asyncio
    async def test_name_is_a_PREFIX_match(self):
        """A practitioner looking for `community.` wants the collection family,
        and an exact name is the degenerate case of the same query."""
        db = _db([])
        await ci.list_package_files(db, name="community.")
        clause = str(db.execute.await_args_list[0].args[0].whereclause)
        assert "like" in clause.lower(), f"name is matched by equality, not a prefix: {clause}"

    @pytest.mark.asyncio
    async def test_it_orders_by_LAST_ACCESS(self):
        """The column retention reads. An operator deciding what to evict wants
        to see what nothing has touched, and ordering by arrival buries exactly
        that."""
        db = _db([])
        await ci.list_package_files(db)
        rendered = str(db.execute.await_args_list[0].args[0])
        assert "last_accessed_at" in rendered.split("ORDER BY")[-1]

    @pytest.mark.asyncio
    async def test_no_filter_means_no_predicate(self):
        db = _db([])
        await ci.list_package_files(db)
        assert db.execute.await_args_list[0].args[0].whereclause is None
