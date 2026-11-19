"""Inventorying artifacts after admission (#2589 step 2b).

The sweep's job is narrow and its failure modes are all quiet: an artifact that
is never inventoried, or one inventoried as an opaque blob because its suffix
was lost on the way out of object storage. Both produce a valid, empty SBOM
rather than an error, so these assert the mechanics that prevent them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import sbom_sweep as sw


class TestSuffixOf:
    """`_is_archive` reads the suffix to decide whether to extract.

    A scratch file named without it makes every archive inventory as an opaque
    file -- the empty-SBOM failure, arriving through a temp filename.
    """

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("cache/pypi/flask/flask-3.0.0-py3-none-any.whl", ".whl"),
            ("cache/npm/lodash/lodash-4.17.21.tgz", ".tgz"),
            # Compound suffixes come back WHOLE: the archive table matches on
            # `.tar.gz`, so returning `.gz` would miss it.
            ("x/y/thing.tar.gz", ".tar.gz"),
            ("x/y/thing.tar.bz2", ".tar.bz2"),
            ("x/y/thing.tar.xz", ".tar.xz"),
            # No suffix at all is the normal shape for a binary, and correct.
            ("cache/binaries/opa/1.0/linux_amd64", ""),
            ("opa", ""),
        ],
    )
    def test_suffix(self, key: str, expected: str) -> None:
        assert sw._suffix_of(key) == expected


@pytest.mark.asyncio
class TestCycle:
    async def test_no_syft_means_no_work_and_no_storage_reads(self) -> None:
        """Asked once, before a batch is taken. Streaming artifacts out of
        object storage to discover syft is missing would be pure waste on a
        sealed node -- and every one of them would come back uninventoried."""
        with patch("terrapod.services.sbom_service.syft_binary", new=AsyncMock(return_value=None)):
            with patch("terrapod.db.session.get_db_session") as session:
                await sw.sbom_cycle()

        assert session.call_count == 0, "a batch was taken with no syft to inventory it"

    async def test_an_uninventoriable_artifact_leaves_the_column_null(self) -> None:
        """NULL is not the same as an empty document. Writing `{}` would make
        the next cycle skip the row for ever, turning a transient failure into a
        permanent absence -- and would record "we looked and found nothing",
        which is a reassuring claim this did not establish.
        """
        row = MagicMock()
        row.sbom = None

        with patch.object(sw, "_inventory_one", new=AsyncMock(return_value=None)):
            await sw._apply(row)

        assert row.sbom is None

    async def test_an_inventory_is_stored(self) -> None:
        row = MagicMock()
        row.sbom = None
        document = {"bomFormat": "CycloneDX", "components": []}

        with patch.object(sw, "_inventory_one", new=AsyncMock(return_value=document)):
            await sw._apply(row)

        assert row.sbom is document


@pytest.mark.asyncio
class TestInventoryOne:
    async def test_a_row_naming_no_object_is_skipped(self) -> None:
        """An admission recorded before the key was carried. Nothing to
        inventory and nothing to retry -- it must not read storage with a key
        of None."""
        row = MagicMock()
        row.admission = {}

        with patch("terrapod.storage.get_storage") as storage:
            assert await sw._inventory_one(row) is None

        assert storage.call_count == 0

    async def test_a_missing_object_degrades_rather_than_raising(self, tmp_path) -> None:
        """The row says where the object is and the store disagrees -- a pruned
        bucket, a partially restored backup. The sweep must survive it; a raise
        here would stop the whole batch on one bad row."""
        row = MagicMock()
        row.admission = {"storage_key": "cache/npm/x/x.tgz"}
        row.digest = "sha256:a"

        store = MagicMock()

        def _boom(_key):
            raise FileNotFoundError("gone")

        store.get_stream = _boom

        with patch("terrapod.storage.get_storage", return_value=store):
            with patch("terrapod.services.api_tool.tool_dir", return_value=tmp_path):
                assert await sw._inventory_one(row) is None


class TestStaleness:
    """What makes a finding stale, which is NOT what makes an SBOM stale.

    An SBOM is a fact about the artifact and never changes. A finding is a
    statement about an SBOM *and* a database epoch, so it expires when the
    database moves. Getting this wrong in the quiet direction -- comparing only
    matcher versions -- means a new CVE never reaches an artifact admitted
    months ago, because no advisory upgrades the binary.

    Asserted on the predicate's STRUCTURE rather than its rendered SQL: the keys
    are bind parameters, so they do not appear in `str()` at all, and a string
    match would pass on a predicate that checks nothing.
    """

    def test_the_database_epoch_adds_a_trigger(self) -> None:
        """Three clauses with an epoch, two without -- the extra one IS the
        database-moved trigger, and without it no advisory ever re-matches."""
        assert len(sw._needs_matching({"updated_at": "2026-11-19T07:06:49Z"}).clauses) == 3

    def test_an_unknown_epoch_does_not_churn_the_whole_ledger(self) -> None:
        """Trivy could not say. Treating that as "everything is stale" would
        re-match the entire ledger on every cycle, for ever."""
        assert len(sw._needs_matching({}).clauses) == 2

    def test_an_empty_epoch_string_is_treated_as_unknown(self) -> None:
        """Not as an epoch of `""`, which would mark every row stale against a
        value nothing can ever equal."""
        assert len(sw._needs_matching({"updated_at": ""}).clauses) == 2
        assert len(sw._needs_matching({"version": 2}).clauses) == 2
