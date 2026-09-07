"""SQLite pricesheet index for cost estimation (#1034).

At multi-region scale (#1025) the published sheet grew to ~260k products /
~84 MB decompressed. The old consumer parsed the **whole** YAML document with
``yaml.safe_load`` (~1.7 GB peak) on **every** cost request, which OOM-kills the
1 Gi API pod and re-does the work each time.

Instead, the sheet is streamed **once** into a SQLite index keyed by
``(type, region)`` — stored in object storage — and both consumers (the
long-lived API *and* the ephemeral runner Job) **query** the pre-built index.
Neither ever holds the whole sheet:

* **Build** (once per cache refresh): :func:`build_index` streams the sheet with
  ``yaml.parse`` — the low-level **event** parser, which emits scalars/mapping
  markers incrementally and never materialises the document — and inserts rows.
  Peak memory is one product + the parser buffer (~23 MB for the full sheet),
  regardless of sheet size.
* **Query** (per estimate): :class:`PricesheetIndex` narrows 260k products to the
  handful sharing a resource's ``type`` + resolved ``region`` (plus region-
  agnostic rows) *before* the existing subset-match runs — bounded memory, and
  a few milliseconds per plan.

A small ``prices_url`` mirror still works the same way: :meth:`build_temp` builds
a **file-backed** index from any stream into a temp file (auto-cleaned), so the
engine has one code path and never holds the DB in memory.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from typing import IO

from terrapod.services.cost.match_set import MatchSet
from terrapod.services.cost.prices import (
    EmptyMatchSet,
    Product,
    _parse_price_type,
)

# type=X is always the first token of a product's match set; region=X lives in
# the pricing set (absent ⇒ region-agnostic, stored as '' and matched everywhere).
_TYPE_RE = re.compile(r"(?:^|&)type=([^&]+)")
_REGION_RE = re.compile(r"(?:^|&)region=([^&]+)")

# record = (type, region, service, family, match, pricing, price, price_type, ccy)
_Record = tuple[str, str, str, str, str, str, str, str, str]


def _record(
    service: str, family: str, match: str, pricing: str, price: str, price_type: str, ccy: str
) -> _Record:
    tm = _TYPE_RE.search(match)
    rm = _REGION_RE.search(pricing)
    return (
        tm.group(1) if tm else "",
        rm.group(1) if rm else "",
        service,
        family,
        match,
        pricing,
        price,
        price_type,
        ccy,
    )


def _stream_yaml_records(first: str, fp: IO[str]) -> Iterator[_Record]:
    """Yield records from a Terrapod YAML sheet via the ``yaml.parse`` **event**
    stream — never materialising the document (the whole point, #1034).

    The document is ``{schema, currency, products: [ {flat product}, ... ]}``.
    We capture ``currency`` from the top-level mapping, then reconstruct each
    product mapping inside the ``products`` sequence one at a time.
    """
    import yaml

    # libyaml's C event parser when available (fast); pure-Python events still
    # stream at bounded memory, just slower — fine for a once-per-refresh build.
    loader = yaml.CSafeLoader if getattr(yaml, "__with_libyaml__", False) else yaml.SafeLoader

    class _Chain:
        """Feed the already-read first line back before the rest of the stream."""

        def __init__(self, head: str, rest: IO[str]):
            self._head: str | None = head
            self._rest = rest

        def read(self, n: int = -1) -> str:
            if self._head is not None:
                head, self._head = self._head, None
                return head + (self._rest.read(n) if n != 0 else "")
            return self._rest.read(n)

    events = yaml.parse(_Chain(first, fp), Loader=loader)
    currency = "USD"
    in_products = False
    top_key: str | None = None
    prod: dict[str, str] | None = None
    field_key: str | None = None

    for ev in events:
        if not in_products:
            if isinstance(ev, yaml.ScalarEvent):
                if top_key is None:
                    top_key = ev.value  # a top-level key (schema/currency/products)
                    if top_key == "products":
                        # the next structural event is the sequence start
                        nxt = next(events, None)
                        if isinstance(nxt, yaml.SequenceStartEvent):
                            in_products = True
                        top_key = None
                else:
                    if top_key == "currency":
                        currency = ev.value
                    top_key = None
            continue
        # inside the products sequence
        if isinstance(ev, yaml.SequenceEndEvent):
            break
        if isinstance(ev, yaml.MappingStartEvent):
            prod, field_key = {}, None
        elif isinstance(ev, yaml.MappingEndEvent):
            if prod is not None and prod.get("match", ""):
                yield _record(
                    prod.get("service", ""),
                    prod.get("family", ""),
                    prod["match"],
                    prod.get("pricing", ""),
                    str(prod.get("price", "")),
                    prod.get("price_type", ""),
                    currency,
                )
            prod = None
        elif isinstance(ev, yaml.ScalarEvent) and prod is not None:
            if field_key is None:
                field_key = ev.value
            else:
                prod[field_key] = ev.value
                field_key = None


def stream_records(fp: IO[str]) -> Iterator[_Record]:
    """Stream ``(type, region, …)`` records from a Terrapod YAML pricesheet.

    Parses the ``schema: terrapod-pricesheet/vN`` document as a ``yaml.parse``
    event stream — bounded memory, the document is never materialised.
    """
    first = fp.readline()
    yield from _stream_yaml_records(first, fp)


_SCHEMA = (
    "CREATE TABLE products("
    "type TEXT, region TEXT, service TEXT, family TEXT, "
    "match TEXT, pricing TEXT, price TEXT, price_type TEXT, ccy TEXT)"
)


def _create(con: sqlite3.Connection, records: Iterator[_Record]) -> int:
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", records)
    con.execute("CREATE INDEX idx_type_region ON products(type, region)")
    con.commit()
    return con.execute("SELECT count(*) FROM products").fetchone()[0]


def build_index(fp: IO[str], db_path: str) -> int:
    """Stream a sheet into a SQLite index file at ``db_path``. Returns row count.

    Bounded memory (streams the sheet; never materialises it). Overwrites any
    existing table content by creating a fresh DB — callers write to a temp path
    then swap.
    """
    con = sqlite3.connect(db_path)
    try:
        return _create(con, stream_records(fp))
    finally:
        con.close()


class PricesheetIndex:
    """Query wrapper over a built SQLite pricesheet index — **always file-backed**.

    The DB is a real file so SQLite pages it off disk (bounded ~18 MB query
    memory), never materialised in RAM. :meth:`candidates` returns the products
    whose ``type`` matches a resource and whose ``region`` is the resource's
    region or region-agnostic — the *only* products that can subset-match it — so
    the engine never scans the full sheet.
    """

    def __init__(self, con: sqlite3.Connection, tmp_path: str | None = None):
        self._con = con
        self._tmp_path = tmp_path  # a build_temp() file to unlink on close()

    @classmethod
    def open(cls, db_path: str) -> PricesheetIndex:
        """Open a pre-built index file (the cached ``.sqlite`` — API/runner)."""
        return cls(sqlite3.connect(db_path, check_same_thread=False))

    @classmethod
    def build_temp(cls, fp: IO[str], dir: str | None = None) -> PricesheetIndex:
        """Build a **file-backed** index from a raw stream into a temp file, for
        tests and small YAML mirrors (the API/runner use a pre-built cached
        file via :meth:`open`). Streamed build (bounded memory); the temp file is
        deleted on :meth:`close`. Never an in-memory DB — the index is always a
        real file so the query stays paged off disk.
        """
        fd, path = tempfile.mkstemp(suffix=".sqlite", dir=dir)
        os.close(fd)
        try:
            build_index(fp, path)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return cls(sqlite3.connect(path, check_same_thread=False), tmp_path=path)

    def candidates(self, rtype: str, region: str | None) -> Iterator[Product]:
        """Yield candidate products for a resource of ``rtype`` in ``region``
        (plus region-agnostic products). The caller runs the subset-match."""
        cur = self._con.execute(
            "SELECT service, family, match, pricing, price, price_type, ccy "
            "FROM products WHERE type = ? AND (region = ? OR region = '')",
            (rtype, region or ""),
        )
        for service, family, match, pricing, price, price_type, ccy in cur:
            try:
                yield Product(
                    service=service,
                    product_family=family,
                    match_set=MatchSet.parse(match),
                    pricing_match_set=MatchSet.parse(pricing),
                    price=_parse_price_type(price_type, float(price)),
                    ccy=ccy,
                )
            except (EmptyMatchSet, ValueError):
                continue

    def close(self) -> None:
        self._con.close()
        if self._tmp_path is not None:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None
