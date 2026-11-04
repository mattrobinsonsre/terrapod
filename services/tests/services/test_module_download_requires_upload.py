"""A module version is downloadable only once its tarball is actually there.

#2368: the versions LIST filtered on `upload_status == "uploaded"` and the
DOWNLOAD did not. So a version created by the first step of a publish -- row
written, tarball not yet uploaded -- was advertised as absent from the list but
still answered the download with `204` and a presigned URL to an object that is
not in storage. `tofu init` then fails inside go-getter with an opaque storage
error rather than the "module version not found" the registry protocol asks for.

The property is a RELATIONSHIP between the two states, so both are instantiated:
a test with only an uploaded fixture passes whether or not the filter is there.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services import registry_module_service


def _db_returning(rows: list[object]) -> MagicMock:
    """A db whose version query yields `rows` -- i.e. the filter is applied by SQL.

    The filter under test lives in the WHERE clause, so what a unit test can
    honestly assert is that the clause is present and names the column; the
    rows here stand for "the database applied it".
    """
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    db.execute = AsyncMock(return_value=result)
    return db


def _module() -> MagicMock:
    m = MagicMock()
    m.id = uuid.uuid4()
    return m


@pytest.mark.asyncio
async def test_the_query_filters_on_upload_status() -> None:
    """The clause itself, because the SQL is where the fix lives."""
    db = _db_returning([MagicMock()])
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://example/x.tar.gz"))

    async def _fake_get_module(*_a, **_k):
        return _module()

    orig = registry_module_service.get_module
    registry_module_service.get_module = _fake_get_module
    try:
        await registry_module_service.get_module_download_url(
            db, storage, "ns", "name", "aws", "1.0.0"
        )
    finally:
        registry_module_service.get_module = orig

    assert db.execute.await_count == 1
    # The WHERE clause, not the whole statement: `str(select(Model))` renders
    # every column of the table, so asserting on the statement passes even with
    # the filter deleted.
    where = str(db.execute.await_args.args[0].whereclause)
    assert "upload_status" in where, (
        "the download query does not filter on upload_status, so a half-published "
        f"version is served a URL to an object that is not there. WHERE: {where}"
    )
    assert "version" in where and "module_id" in where


@pytest.mark.asyncio
async def test_a_version_the_database_withholds_is_not_found() -> None:
    """The other state: the filter matched nothing, so there is no URL to give."""
    db = _db_returning([])
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock()

    async def _fake_get_module(*_a, **_k):
        return _module()

    orig = registry_module_service.get_module
    registry_module_service.get_module = _fake_get_module
    try:
        url = await registry_module_service.get_module_download_url(
            db, storage, "ns", "name", "aws", "1.0.0"
        )
    finally:
        registry_module_service.get_module = orig

    assert url is None
    storage.presigned_get_url.assert_not_awaited()
