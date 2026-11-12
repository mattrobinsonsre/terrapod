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
        """The ceiling, with enough objects for it to BITE.

        This seeded 20 and asked for 10**9, which told us nothing: 20 is under
        `MAX_ARCHIVE_SCAN` either way, so `min(..., MAX_ARCHIVE_SCAN)` could be
        deleted and the assertion still held. The ceiling is the only thing
        standing between `?limit=999999999` and a six-figure estate in one
        response, so it needs a fixture it can actually clamp.
        """
        over = ci.MAX_ARCHIVE_SCAN + 7
        objs = [_Obj(f"vcs_archives/c/o/r/s{i:06d}-full.tar.gz", 1) for i in range(over)]
        entries, cursor = await ci.list_archives(_Storage(objs), limit=10**9)
        assert len(entries) == ci.MAX_ARCHIVE_SCAN, (
            f"asked for 10**9 over {over} objects and got {len(entries)}; "
            "the ceiling is not being applied"
        )
        # A full page hands back a cursor, so the caller can walk the rest.
        assert cursor == entries[-1].key

    @pytest.mark.asyncio
    async def test_a_limit_under_the_ceiling_is_honoured_as_asked(self):
        """The other half: the clamp must not become a floor.

        Without this, `bounded = MAX_ARCHIVE_SCAN` would pass the test above.
        """
        objs = [_Obj(f"vcs_archives/c/o/r/s{i:05d}-full.tar.gz", 1) for i in range(20)]
        entries, _ = await ci.list_archives(_Storage(objs), limit=5)
        assert len(entries) == 5

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
    async def test_a_cache_EXACTLY_at_the_ceiling_is_exact_rather_than_truncated(self, monkeypatch):
        """The boundary, which nothing distinguished.

        A full page cannot, by itself, tell "this is everything" from "the walk
        stopped" -- so `>=` flagged a cache holding precisely the ceiling as
        truncated and put "these numbers understate the problem" over figures
        that were exact. `archive_totals` asks for one object PAST the ceiling to
        separate the two, which is the only way the comparison can mean anything;
        with the old limit, `>` could never be true at all and the flag was dead.

        Both halves are here, because either alone passes under one of the two
        wrong forms.
        """
        monkeypatch.setattr(ci, "MAX_ARCHIVE_SCAN", 3)
        objs = [_Obj(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 1) for i in range(3)]
        totals, truncated = await ci.archive_totals(_Storage(objs))
        assert truncated is False, "a complete walk is reported as truncated"
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


class TestABoundedArchiveSweepSaysSo:
    """#2491: a bulk eviction stopped at a ceiling and reported no truncation,
    so a cache larger than it could never be purged and nothing said so.

    The observable sequence was worse than a partial result: an operator posts
    `{"older_than_days": 30}` against 20,000 archives, gets back
    `{"status": "evicted", "archives": 400}`, reloads the totals, sees the same
    size, and reads it as "the purge did nothing" rather than "the purge is
    bounded".
    """

    @pytest.mark.asyncio
    async def test_a_sweep_that_stopped_EARLY_reports_truncated(self, monkeypatch):
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 3)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i:02d}-full.tar.gz", 100) for i in range(10)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage)
        assert result.files == 3, "the sweep was not bounded"
        assert result.truncated is True, (
            "a bounded sweep reported itself complete -- the operator reloads the "
            "totals, sees the same size, and reads it as the purge having done nothing"
        )

    @pytest.mark.asyncio
    async def test_a_sweep_that_FINISHED_reports_truncated_FALSE(self, monkeypatch):
        """The control for the test above. An assertion that nothing was
        truncated is worth nothing unless the same code reports truncation when
        there is some -- and `truncated = True` unconditionally would satisfy
        every other test in this class."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 10)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i:02d}-full.tar.gz", 100) for i in range(3)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage)
        assert (result.files, result.truncated, result.cursor) == (3, False, "")

    @pytest.mark.asyncio
    async def test_a_cache_EXACTLY_at_the_bound_is_complete_rather_than_truncated(
        self, monkeypatch
    ):
        """The walk asks for one key PAST the window, which is what makes the
        flag exact. Asking for exactly the window cannot tell "this many exist"
        from "more exist and we stopped": both come back full, so `>=` would
        tell an operator more remained when nothing did."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 3)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 100) for i in range(3)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage)
        assert (result.files, result.truncated, result.cursor) == (3, False, "")

    @pytest.mark.asyncio
    async def test_the_cursor_is_the_last_key_EXAMINED_not_the_last_DELETED(self, monkeypatch):
        """The whole mechanism. An age filter may match a handful of keys in a
        window of a thousand, so resuming past the last DELETION would re-walk
        every non-matching key in between -- which is the stuck-window defect in
        a smaller costume. Resuming past the last key LOOKED AT is what reaches
        keys the call never saw."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 3)
        old = _Obj("vcs_archives/c/o/r/k0-full.tar.gz", 10, _now() - timedelta(days=30))
        objs = [old] + [_Obj(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 10, _now()) for i in (1, 2, 3)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage, older_than_days=7)
        assert storage.deleted == ["vcs_archives/c/o/r/k0-full.tar.gz"]
        assert result.truncated is True
        assert result.cursor == "vcs_archives/c/o/r/k2-full.tar.gz", (
            "the cursor is the last DELETION, so the next call re-walks k1 and k2"
        )

    @pytest.mark.asyncio
    async def test_a_window_where_NOTHING_matches_still_advances(self, monkeypatch):
        """The degenerate case, and the one that was unreachable for ever: with
        no key in the first window old enough, the old code deleted nothing, so
        the window never moved and the rest of the estate could not be touched
        however many times the route was run."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 2)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 10, _now()) for i in range(5)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage, older_than_days=7)
        assert (result.files, storage.deleted) == (0, [])
        assert result.truncated is True
        assert result.cursor == "vcs_archives/c/o/r/k1-full.tar.gz", (
            "a window that matched nothing did not advance, so the sweep is stuck"
        )

    @pytest.mark.asyncio
    async def test_repeating_with_the_cursor_reaches_the_WHOLE_estate(self, monkeypatch):
        """End to end, which is the claim #2491 actually makes: a cache larger
        than one window is purgeable by repeating the call. Only the LAST key is
        old enough, so the sweep has to walk past four windows of keys it keeps
        to reach it -- the exact shape that never terminated before."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 2)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 10, _now()) for i in range(8)]
        objs.append(_Obj("vcs_archives/c/o/r/zz-full.tar.gz", 70, _now() - timedelta(days=30)))
        storage = _Storage(objs)

        after, calls, removed = "", 0, 0
        while True:
            calls += 1
            assert calls < 20, "the sweep did not terminate"
            result = await ci.evict_archives(storage, older_than_days=7, after=after)
            removed += result.files
            if not result.truncated:
                break
            assert result.cursor, "truncated with no cursor: nothing can resume it"
            assert result.cursor > after, "the cursor went backwards or stalled"
            after = result.cursor

        assert removed == 1
        assert storage.deleted == ["vcs_archives/c/o/r/zz-full.tar.gz"]

    @pytest.mark.asyncio
    async def test_the_window_is_the_EVICT_bound_not_the_SCAN_ceiling(self, monkeypatch):
        """Two constants, deliberately. `MAX_ARCHIVE_SCAN` bounds a READ -- how
        much of the estate a listing shows or a total aggregates, which costs one
        list call -- and `MAX_EVICT_BATCH` bounds a destructive LOOP, where every
        entry in the window is a round trip. Merging them is the obvious tidy and
        would be wrong in both directions."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 2)
        monkeypatch.setattr(ci, "MAX_ARCHIVE_SCAN", 100)
        objs = [_Obj(f"vcs_archives/c/o/r/k{i}-full.tar.gz", 10) for i in range(50)]
        storage = _Storage(objs)
        result = await ci.evict_archives(storage)
        assert result.files == 2, "the eviction used the read ceiling as its bound"


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


def _db(rows, total=None):
    """A session double that answers BOTH shapes `list_package_files` issues.

    It runs two statements now -- a `COUNT` and then the page -- so one result
    object has to serve `scalar()` as well as `scalars().all()`. Returning the
    same object for every execute rather than scripting an ordered list is
    deliberate: a scripted list desynchronises every result after it the moment
    a function gains a query, which is a `StopIteration` naming nothing about
    the cause.

    `total` defaults to the number of rows, so a test that does not care about
    pagination reads as it did before.
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    result.scalar.return_value = len(rows) if total is None else total
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
    async def test_it_stops_at_the_bound_and_REPORTS_that_it_did(self, monkeypatch):
        """#2492: this used to delete every matching object sequentially inside
        one handler. 50,000 artifacts at ~30 ms each is about 25 minutes, so the
        gateway timed out, the transaction rolled back, and the objects already
        deleted left rows pointing at keys that no longer existed.

        The bound is what makes the request finish, and `truncated` is what makes
        the bound safe to have: without it the operator cannot tell a finished
        purge from a bounded one.
        """
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 3)
        rows = [_file(id=f"f{i}", size=10) for i in range(10)]
        db = _db(rows)
        storage = MagicMock()
        storage.delete = AsyncMock()
        result = await ci.evict_package_files(db, storage)
        assert result.files == 3, "the eviction was not bounded"
        assert storage.delete.await_count == 3
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_a_sweep_INSIDE_the_bound_reports_truncated_FALSE(self, monkeypatch):
        """The control. The query asks for one row PAST the bound, so a match of
        exactly the bound is reported as complete rather than as "the page was
        full" -- otherwise an operator is told more remains when nothing does."""
        monkeypatch.setattr(ci, "MAX_EVICT_BATCH", 3)
        rows = [_file(id=f"f{i}", size=10) for i in range(3)]
        db = _db(rows)
        storage = MagicMock()
        storage.delete = AsyncMock()
        result = await ci.evict_package_files(db, storage)
        assert (result.files, result.truncated) == (3, False)

    @pytest.mark.asyncio
    async def test_it_carries_NO_cursor_because_it_resumes_by_itself(self):
        """The asymmetry with the archive sweep, pinned rather than tidied: this
        one deletes the rows it selected, so the next call's own bound picks up
        where this stopped. A cursor here would be an offset into a shrinking
        set, which skips rows."""
        rows = [_file(id="a", size=1)]
        db = _db(rows)
        storage = MagicMock()
        storage.delete = AsyncMock()
        result = await ci.evict_package_files(db, storage)
        assert result.cursor == ""

    @pytest.mark.asyncio
    async def test_the_bound_takes_the_STALEST_first(self):
        """Which matters only because the call is bounded: if a sweep can take a
        thousand of fifty thousand, the thousand worth taking are the ones
        nothing has touched for longest. The scheduled sweep orders its own batch
        the same way and says so.

        Asserted on the SQL, because the double returns whatever rows it was
        given regardless of the ordering the query asked for.
        """
        db = _db([])
        storage = MagicMock()
        storage.delete = AsyncMock()
        await ci.evict_package_files(db, storage)
        order = str(db.execute.await_args_list[0].args[0]).split("ORDER BY")[-1]
        assert "last_accessed_at" in order
        assert "DESC" not in order.upper(), f"the sweep takes the freshest first: {order}"

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
        that.

        Read off the SECOND statement, because the first is now the `COUNT`
        that `meta.pagination.total-count` comes from and a count has no
        ordering to assert about.
        """
        db = _db([])
        await ci.list_package_files(db)
        rendered = str(db.execute.await_args_list[1].args[0])
        assert "last_accessed_at" in rendered.split("ORDER BY")[-1]

    @pytest.mark.asyncio
    async def test_the_order_breaks_TIES_so_a_row_cannot_land_on_two_pages(self):
        """`last_accessed_at` alone is not a total order: artifacts cached by one
        request share a timestamp to the microsecond, so without a tiebreak
        Postgres may order them differently per query -- and under `LIMIT` /
        `OFFSET` that means a row appearing on two consecutive pages, or on
        neither. Invisible at the fixture sizes the rest of this file uses,
        which is why it is asserted on the SQL."""
        db = _db([])
        await ci.list_package_files(db, limit=10)
        order = str(db.execute.await_args_list[1].args[0]).split("ORDER BY")[-1]
        assert "last_accessed_at" in order
        assert "id" in order, f"no tiebreak after last_accessed_at: {order}"

    @pytest.mark.asyncio
    async def test_the_page_is_sliced_in_SQL_rather_than_in_python(self):
        """The defect this closes (#2492): every page of the listing used to
        fetch EVERY matching row and slice the list in Python, so a page of a
        60,000-artifact cache materialised all 60,000 ORM rows -- and the admin
        panel walked every page, which made one filter keystroke hundreds of
        requests each paying for the whole table.

        Asserted on the statement rather than on the returned length, because a
        double that returns a fixed list regardless would satisfy a
        length-based assertion whether or not the bound reached the database.
        """
        db = _db([])
        await ci.list_package_files(db, limit=50, offset=100)
        rendered = str(db.execute.await_args_list[1].args[0]).lower()
        assert "limit" in rendered, f"the page is not bounded in SQL: {rendered}"
        assert "offset" in rendered, f"the page is not offset in SQL: {rendered}"

    @pytest.mark.asyncio
    async def test_no_limit_still_returns_EVERYTHING(self):
        """The documented non-paged contract: `page[size]` absent means the full
        result set, and an SDK that sends no paging params relies on it. So the
        fix had to make a PAGE cost a page without changing what a caller
        asking for no page gets."""
        db = _db([])
        await ci.list_package_files(db)
        rendered = str(db.execute.await_args_list[1].args[0]).lower()
        assert "limit" not in rendered, f"an unpaged request was bounded: {rendered}"

    @pytest.mark.asyncio
    async def test_the_total_is_COUNTED_rather_than_measured_from_the_page(self):
        """`meta.pagination.total-count` has to be the whole match, not the page
        -- a page of 50 out of 60,000 reported as a total of 50 tells a client
        there is nothing more to fetch."""
        db = _db([_file()], total=60000)
        entries, total = await ci.list_package_files(db, limit=50)
        assert len(entries) == 1
        assert total == 60000
        first = str(db.execute.await_args_list[0].args[0]).lower()
        assert "count" in first, f"the first statement is not a count: {first}"

    @pytest.mark.asyncio
    async def test_no_filter_means_no_predicate(self):
        db = _db([])
        await ci.list_package_files(db)
        assert db.execute.await_args_list[0].args[0].whereclause is None
