"""Tests for the cost-estimation pricesheet cache service (#871).

Exercises the real download→gunzip→store path (with a fake httpx client and an
AsyncMock storage that consumes the streamed chunks), plus the pull-through
``ensure_pricesheet`` freshness logic (missing / fresh / stale / stale-fallback).
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import cost_pricesheet_service as svc

_YAML = (
    b"schema: terrapod-pricesheet/v1\n"
    b"currency: USD\n"
    b"products:\n"
    b"- service: AmazonEC2\n"
    b"  family: Compute\n"
    b"  match: type=aws_instance\n"
    b"  pricing: region=us-east-1\n"
    b"  price: '0.10'\n"
    b"  price_type: t\n"
)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self, chunk_size: int | None = None):
        mid = len(self._data) // 2
        yield self._data[:mid]
        yield self._data[mid:]


class _FakeStreamCtx:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> _FakeResp:
        return _FakeResp(self._data)

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeGet:
    """Minimal stand-in for an httpx response to a plain GET."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """A fake httpx client that RECORDS the URLs it is asked for.

    Recording matters: the sibling-digest lookup is only meaningful if the test
    can assert *which* URL was fetched. A fake that answered without recording
    would let the code ask for the wrong thing and still pass.

    `sibling` is what `<url>.sha256` returns — `None` meaning the asset does not
    exist, which is the air-gapped-mirror case and must not be fatal.
    """

    def __init__(self, data: bytes, sibling: _FakeGet | None = None) -> None:
        self._data = data
        self._sibling = sibling
        self.get_urls: list[str] = []
        self.stream_urls: list[str] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method: str, url: str, timeout: float | None = None) -> _FakeStreamCtx:
        self.stream_urls.append(url)
        return _FakeStreamCtx(self._data)

    async def get(self, url: str, timeout: float | None = None) -> _FakeGet:
        self.get_urls.append(url)
        return self._sibling if self._sibling is not None else _FakeGet(404, "")


def _storage_capturing(captured: dict) -> AsyncMock:
    async def fake_put_stream(key, chunks, content_type="application/octet-stream", metadata=None):
        data = b""
        async for chunk in chunks:
            data += chunk
        captured["key"] = key
        captured["data"] = data
        captured["content_type"] = content_type
        return MagicMock()

    storage = AsyncMock()
    storage.put_stream = fake_put_stream
    return storage


def _meta(age: timedelta) -> MagicMock:
    m = MagicMock()
    m.last_modified = datetime.now(UTC) - age
    return m


# --- refresh (real gunzip + streamed store) --------------------------------


async def test_refresh_downloads_builds_sqlite_index_and_stores():
    # refresh now gunzips → builds a SQLite index → stores the .sqlite (#1034).
    gz = gzip.compress(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)

    with patch.object(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(gz)):
        size = await svc.refresh_pricesheet(storage)

    assert captured["key"] == "cache/cost/prices.sqlite"
    assert captured["content_type"] == "application/x-sqlite3"
    assert size == len(captured["data"]) > 0
    assert captured["data"][:16].startswith(b"SQLite format 3")  # real sqlite file

    # the stored index is queryable and has the sheet's product
    import os
    import tempfile

    from terrapod.services.cost.pricesheet_db import PricesheetIndex

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(captured["data"])
        idx = PricesheetIndex.open(path)
        cands = list(idx.candidates("aws_instance", "us-east-1"))
        assert len(cands) == 1 and cands[0].price.value == 0.10
        idx.close()
    finally:
        os.unlink(path)


async def test_pricesheet_available_and_download_url():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    presigned = MagicMock()
    presigned.url = "https://example/presigned/prices.sqlite"
    storage.presigned_get_url = AsyncMock(return_value=presigned)

    assert await svc.pricesheet_available(storage) is True
    assert await svc.pricesheet_download_url(storage) == "https://example/presigned/prices.sqlite"
    storage.exists.assert_awaited_with("cache/cost/prices.sqlite")


# --- pull-through ensure_pricesheet ----------------------------------------


async def test_ensure_fetches_when_missing():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=False)
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_awaited_once()


async def test_ensure_skips_when_fresh():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(hours=1)))  # fresh
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_not_awaited()


async def test_ensure_refetches_when_stale():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(days=2)))  # stale
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        assert await svc.ensure_pricesheet(storage) is True
        refresh.assert_awaited_once()


async def test_ensure_serves_stale_copy_when_refresh_fails():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(days=2)))  # stale
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        refresh.side_effect = RuntimeError("upstream down")
        # A stale copy exists → best-effort serves it (True), no raise.
        assert await svc.ensure_pricesheet(storage) is True


async def test_ensure_returns_false_when_no_copy_and_fetch_fails():
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=False)
    with patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh:
        refresh.side_effect = RuntimeError("upstream down")
        assert await svc.ensure_pricesheet(storage) is False


# --- integrity + bounds (GHSA-7486-vv55-jxhq) -------------------------------
#
# Every test here drives `refresh_pricesheet`, not the helpers it calls. The
# helpers are easy to call directly and that would prove nothing: the defect was
# that the real path had no cap and no digest check, so the real path is what has
# to be exercised. `put_stream` never running is the assertion that matters —
# it means the previously cached sheet is still what serves.


def _gz(payload: bytes) -> bytes:
    return gzip.compress(payload)


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class _Caps:
    """Set the three new settings for the duration of a test, then restore."""

    def __init__(self, *, pinned: str = "", cmax: int | None = None, dmax: int | None = None):
        self._want = {
            "prices_sha256": pinned,
            "prices_max_compressed_bytes": cmax,
            "prices_max_decompressed_bytes": dmax,
        }
        self._old: dict[str, object] = {}

    def __enter__(self):
        cfg = svc.settings.cost_estimation
        for k, v in self._want.items():
            if v is None:
                continue
            self._old[k] = getattr(cfg, k)
            setattr(cfg, k, v)
        return cfg

    def __exit__(self, *exc):
        cfg = svc.settings.cost_estimation
        for k, v in self._old.items():
            setattr(cfg, k, v)
        return False


async def test_an_oversize_compressed_download_is_refused_and_nothing_is_cached():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    # One byte under the real size, so the cap trips on the final chunk rather
    # than the first — the interesting case, since a cap checked only once at the
    # start would pass this.
    with (
        _Caps(cmax=len(gz) - 1),
        patch.object(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(gz)),
    ):
        try:
            await svc.refresh_pricesheet(storage)
        except svc.PricesheetRejected as exc:
            assert "prices_max_compressed_bytes" in str(exc)
        else:
            raise AssertionError("an oversize download was accepted")
    assert captured == {}, "a refused download must not reach object storage"


async def test_a_decompression_bomb_is_refused_before_it_lands():
    # Tiny compressed, large decompressed — the shape that makes a bomb cheap.
    bomb = _gz(b"x" * 500_000)
    assert len(bomb) < 2_000, "fixture is not actually a bomb"
    captured: dict = {}
    storage = _storage_capturing(captured)
    with (
        _Caps(cmax=10 * 1024 * 1024, dmax=4096),
        patch.object(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(bomb)),
    ):
        try:
            await svc.refresh_pricesheet(storage)
        except svc.PricesheetRejected as exc:
            assert "prices_max_decompressed_bytes" in str(exc)
        else:
            raise AssertionError("a decompression bomb was accepted")
    assert captured == {}


async def test_a_configured_digest_mismatch_is_refused_before_decompression():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    gunzipped: list[str] = []
    real = svc._gunzip_file

    def spy(gz_path, out_path, max_bytes):
        gunzipped.append(out_path)
        return real(gz_path, out_path, max_bytes)

    with (
        _Caps(pinned="0" * 64),
        patch.object(svc.httpx, "AsyncClient", lambda **kw: _FakeClient(gz)),
        patch.object(svc, "_gunzip_file", spy),
    ):
        try:
            await svc.refresh_pricesheet(storage)
        except svc.PricesheetRejected as exc:
            assert "digest mismatch" in str(exc)
        else:
            raise AssertionError("a mismatched digest was accepted")
    assert captured == {}
    # Ordering is load-bearing, not incidental: a sheet that fails its digest is
    # never decompressed, because a mismatch and a bomb are plausibly one event.
    assert gunzipped == [], "a sheet that failed its digest was decompressed anyway"


async def test_a_configured_digest_wins_and_the_sibling_is_not_fetched():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    client = _FakeClient(gz, _FakeGet(200, "f" * 64))  # would MISMATCH if consulted
    with _Caps(pinned=_sha(gz)), patch.object(svc.httpx, "AsyncClient", lambda **kw: client):
        size = await svc.refresh_pricesheet(storage)
    assert size > 0 and captured["key"] == "cache/cost/prices.sqlite"
    # The operator's value is authoritative; consulting the sibling at all would
    # let whoever controls the artifact override the deployment's own pin.
    assert client.get_urls == []


async def test_the_sibling_digest_is_fetched_from_the_right_url_and_enforced():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    client = _FakeClient(gz, _FakeGet(200, "a" * 64))
    with _Caps(pinned=""), patch.object(svc.httpx, "AsyncClient", lambda **kw: client):
        try:
            await svc.refresh_pricesheet(storage)
        except svc.PricesheetRejected as exc:
            assert "digest mismatch" in str(exc)
        else:
            raise AssertionError("a mismatched sibling digest was accepted")
    assert captured == {}
    assert client.get_urls == [svc.settings.cost_estimation.prices_url + ".sha256"]


async def test_the_sibling_digest_is_accepted_in_sha256sum_format():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    # `sha256sum` writes "<hex>  <name>"; the publisher uses exactly this, so a
    # parser that only accepted a bare digest would reject every real sheet.
    client = _FakeClient(gz, _FakeGet(200, f"{_sha(gz)}  prices.yaml.gz\n"))
    with _Caps(pinned=""), patch.object(svc.httpx, "AsyncClient", lambda **kw: client):
        size = await svc.refresh_pricesheet(storage)
    assert size > 0 and captured["key"] == "cache/cost/prices.sqlite"


async def test_a_missing_sibling_digest_is_not_fatal():
    # The air-gapped-mirror case: `prices_url` may serve the sheet and nothing
    # else. Refusing here would break every mirror that predates this change.
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    client = _FakeClient(gz)  # 404 on the sibling
    with _Caps(pinned=""), patch.object(svc.httpx, "AsyncClient", lambda **kw: client):
        size = await svc.refresh_pricesheet(storage)
    assert size > 0 and captured["key"] == "cache/cost/prices.sqlite"
    assert client.get_urls == [svc.settings.cost_estimation.prices_url + ".sha256"]


async def test_a_malformed_sibling_digest_is_ignored_rather_than_trusted():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    # Not 64 hex characters — an HTML error page served with a 200, say. Treating
    # that as a digest would reject every legitimate sheet.
    client = _FakeClient(gz, _FakeGet(200, "<html>404 not found</html>"))
    with _Caps(pinned=""), patch.object(svc.httpx, "AsyncClient", lambda **kw: client):
        size = await svc.refresh_pricesheet(storage)
    assert size > 0


async def test_a_matching_digest_is_reported_in_the_log_with_its_source():
    gz = _gz(_YAML)
    captured: dict = {}
    storage = _storage_capturing(captured)
    client = _FakeClient(gz, _FakeGet(200, _sha(gz)))
    with (
        _Caps(pinned=""),
        patch.object(svc.httpx, "AsyncClient", lambda **kw: client),
        patch.object(svc.logger, "info") as log,
    ):
        await svc.refresh_pricesheet(storage)
    kwargs = log.call_args.kwargs
    assert kwargs["sha256"] == _sha(gz)
    # Which check actually ran is the thing an operator needs from the log: a
    # "sibling" refresh is weaker than a "configured" one and reads identically
    # without this.
    assert kwargs["digest_source"] == "sibling"
    assert kwargs["compressed_bytes"] == len(gz)
    assert kwargs["decompressed_bytes"] == len(_YAML)


async def test_a_rejected_sheet_is_logged_distinctly_from_an_outage():
    # An operator has to be able to tell "upstream is down" from "upstream served
    # us something we refused", because only the second might be an attack. Both
    # serve the stale copy, so the log line is the only thing that distinguishes
    # them — and `cost_pricesheet_rejected` is named in docs/cost-estimation.md.
    storage = AsyncMock()
    storage.exists = AsyncMock(return_value=True)
    storage.head = AsyncMock(return_value=_meta(timedelta(days=3)))  # stale

    with (
        patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh,
        patch.object(svc.logger, "warning") as warn,
    ):
        refresh.side_effect = svc.PricesheetRejected("pricesheet digest mismatch: ...")
        assert await svc.ensure_pricesheet(storage) is True  # stale copy still serves
    assert warn.call_args.args[0] == "cost_pricesheet_rejected"
    assert warn.call_args.kwargs["served_stale"] is True

    with (
        patch.object(svc, "refresh_pricesheet", new_callable=AsyncMock) as refresh,
        patch.object(svc.logger, "warning") as warn,
    ):
        refresh.side_effect = RuntimeError("connection reset")
        assert await svc.ensure_pricesheet(storage) is True
    assert warn.call_args.args[0] == "cost_pricesheet_refresh_failed"
