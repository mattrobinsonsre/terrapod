"""Unit tests for the shared list-pagination helper.

Exercises the whole convention matrix directly on the pure helper (no DB, no
HTTP): absent params → full list, page[size]=0 → full list, page[size]>=1 →
that page, page[number] windowing, past-the-end empty page, MAX_PAGE_SIZE cap,
and that meta.pagination always carries the four Terrapod keys.
"""

from types import SimpleNamespace

from terrapod.api.pagination import MAX_PAGE_SIZE, paginate, parse_page_params


def _req(params: dict | None):
    """A minimal stand-in for a Starlette Request exposing query_params.get."""
    return SimpleNamespace(query_params=params or {})


def _items(n: int) -> list[int]:
    return list(range(n))


# ── parse_page_params ────────────────────────────────────────────────────


class TestParsePageParams:
    def test_absent_is_non_paged(self):
        number, size = parse_page_params(_req({}))
        assert number == 1
        assert size is None  # None → "return the full list"

    def test_size_zero_is_non_paged(self):
        # Explicit page[size]=0 is the second "give me everything" signal.
        _, size = parse_page_params(_req({"page[size]": "0"}))
        assert size is None

    def test_negative_size_is_non_paged(self):
        _, size = parse_page_params(_req({"page[size]": "-5"}))
        assert size is None

    def test_size_present(self):
        number, size = parse_page_params(_req({"page[size]": "25"}))
        assert (number, size) == (1, 25)

    def test_number_present(self):
        number, size = parse_page_params(_req({"page[number]": "3", "page[size]": "10"}))
        assert (number, size) == (3, 10)

    def test_number_below_one_clamps(self):
        number, _ = parse_page_params(_req({"page[number]": "0", "page[size]": "10"}))
        assert number == 1

    def test_garbage_values_ignored(self):
        number, size = parse_page_params(_req({"page[size]": "abc", "page[number]": "x"}))
        assert (number, size) == (1, None)

    def test_none_request(self):
        number, size = parse_page_params(None)
        assert (number, size) == (1, None)


# ── paginate: non-paged (full list) ──────────────────────────────────────


class TestPaginateNonPaged:
    def test_absent_returns_full_list_single_page(self):
        items = _items(37)
        page, meta = paginate(items, _req({}))
        assert page == items  # every element, one page
        assert meta["pagination"] == {
            "current-page": 1,
            "page-size": 37,
            "total-count": 37,
            "total-pages": 1,
        }

    def test_size_zero_returns_full_list(self):
        items = _items(5)
        page, meta = paginate(items, _req({"page[size]": "0"}))
        assert page == items
        assert meta["pagination"]["total-pages"] == 1
        assert meta["pagination"]["total-count"] == 5

    def test_empty_list_non_paged(self):
        page, meta = paginate([], _req({}))
        assert page == []
        # page-size falls back to 1 so a client never divides by zero
        assert meta["pagination"] == {
            "current-page": 1,
            "page-size": 1,
            "total-count": 0,
            "total-pages": 1,
        }


# ── paginate: paged ──────────────────────────────────────────────────────


class TestPaginatePaged:
    def test_first_page(self):
        items = _items(10)
        page, meta = paginate(items, _req({"page[size]": "3"}))
        assert page == [0, 1, 2]
        assert meta["pagination"] == {
            "current-page": 1,
            "page-size": 3,
            "total-count": 10,
            "total-pages": 4,  # ceil(10/3)
        }

    def test_second_page(self):
        items = _items(10)
        page, meta = paginate(items, _req({"page[size]": "3", "page[number]": "2"}))
        assert page == [3, 4, 5]
        assert meta["pagination"]["current-page"] == 2

    def test_last_partial_page(self):
        items = _items(10)
        page, _ = paginate(items, _req({"page[size]": "3", "page[number]": "4"}))
        assert page == [9]  # only one element left

    def test_page_past_end_is_empty_not_error(self):
        items = _items(10)
        page, meta = paginate(items, _req({"page[size]": "3", "page[number]": "99"}))
        assert page == []
        assert meta["pagination"]["total-count"] == 10  # count still truthful

    def test_size_capped_at_max(self):
        items = _items(500)
        page, meta = paginate(items, _req({"page[size]": str(MAX_PAGE_SIZE + 50)}))
        assert len(page) == MAX_PAGE_SIZE
        assert meta["pagination"]["page-size"] == MAX_PAGE_SIZE
        assert meta["pagination"]["total-count"] == 500

    def test_exact_multiple_total_pages(self):
        items = _items(9)
        _, meta = paginate(items, _req({"page[size]": "3"}))
        assert meta["pagination"]["total-pages"] == 3  # 9/3 exact

    def test_total_count_reflects_full_input_not_page(self):
        # total-count must be the whole collection, not the returned slice —
        # this is what a looping client keys off to know it's done.
        items = _items(10)
        _, meta = paginate(items, _req({"page[size]": "3"}))
        assert meta["pagination"]["total-count"] == 10
