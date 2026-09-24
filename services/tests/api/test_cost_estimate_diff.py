"""The cost-estimate ingest caches the monthly *delta*, not just the total.

`Diff` is already in the artifact the runner uploads (cost_estimate.go: "Diff is
the monthly delta this run introduces"), but only `Total` was cached on the run.
The PR status comment needs the delta, and re-deriving it per comment render is
work for a value that cannot change once the plan is done.
"""

import json

from terrapod.api.routers.run_artifacts import _summarize_cost_file


def _write(tmp_path, payload) -> str:
    p = tmp_path / "cost.json"
    p.write_text(json.dumps(payload))
    return str(p)


class TestSummarizeCostFileDiff:
    def test_returns_diff_alongside_total(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "currency": "GBP",
                "total": {"min": 12400.0, "max": 12900.0},
                "diff": {"min": 412.0, "max": 500.0},
            },
        )
        summary = _summarize_cost_file(path)
        assert summary is not None
        assert summary.currency == "GBP"
        assert summary.monthly_min == 12400.0
        assert summary.monthly_max == 12900.0
        assert summary.diff_min == 412.0
        assert summary.diff_max == 500.0

    def test_negative_diff_preserved(self, tmp_path):
        """A plan that removes resources has a negative delta."""
        path = _write(
            tmp_path,
            {
                "currency": "USD",
                "total": {"min": 100.0, "max": 100.0},
                "diff": {"min": -18.0, "max": -18.0},
            },
        )
        summary = _summarize_cost_file(path)
        assert summary is not None
        assert summary.diff_min == -18.0

    def test_missing_diff_leaves_none_but_keeps_total(self, tmp_path):
        """Older artifacts have no `diff` block; the total must still cache."""
        path = _write(
            tmp_path,
            {"currency": "EUR", "total": {"min": 5.0, "max": 5.0}},
        )
        summary = _summarize_cost_file(path)
        assert summary is not None
        assert summary.monthly_min == 5.0
        assert summary.diff_min is None
        assert summary.diff_max is None

    def test_unparseable_still_returns_none(self, tmp_path):
        """Existing contract: a broken artifact skips caching, never raises."""
        p = tmp_path / "cost.json"
        p.write_text("not json at all")
        assert _summarize_cost_file(str(p)) is None
