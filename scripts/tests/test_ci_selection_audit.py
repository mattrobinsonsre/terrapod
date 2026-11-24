"""The CI-selection audit's `gh` calls are retried on a transient failure.

The audit makes one API read per replayed push, so with `--runs 30` a single
upstream 502 failed the whole scheduled run -- and a red `CI Selection Audit`
reads as "the CI selection gate is unsound" rather than as a registry hiccup,
which is the expensive part of it.

Loaded by path for the same reason `conftest.py` loads its own: `scripts/` is
not a package, and `ci-selection-audit` is not an importable name anyway.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_selection_audit",
    pathlib.Path(__file__).resolve().parents[1] / "ci-selection-audit.py",
)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


class TestClassifyingAGhFailure:
    """Positive evidence only: an unrecognised failure stays FINAL.

    Retrying by default would make a 404 or an auth error wait out the whole
    backoff before reporting, and a deny-list of definitive shapes cannot stay
    complete against someone else's CLI.
    """

    @pytest.mark.parametrize(
        ("stderr", "why"),
        [
            ("gh: Server Error (HTTP 502)", "the one that actually fired"),
            ("gh: Service Unavailable (HTTP 503)", "5xx"),
            ("gh: Gateway Timeout (HTTP 504)", "5xx"),
            ("HTTP 429: API rate limit exceeded", "asked to come back"),
            ("dial tcp 1.2.3.4:443: i/o timeout", "transport"),
            ("net/http: TLS handshake timeout", "transport"),
            ("read: connection reset by peer", "transport"),
        ],
    )
    def test_a_transient_failure_is_recognised(self, stderr: str, why: str) -> None:
        assert audit._GH_TRANSIENT.search(stderr), why

    @pytest.mark.parametrize(
        ("stderr", "why"),
        [
            ("gh: Not Found (HTTP 404)", "asking again asks the same missing thing"),
            ("gh: Bad credentials (HTTP 401)", "a token does not become valid by waiting"),
            ("gh: Forbidden (HTTP 403)", "nor does a permission"),
            ("gh: a message nobody has written yet", "unrecognised stays final"),
            ("", "no stderr at all"),
        ],
    )
    def test_everything_else_is_final(self, stderr: str, why: str) -> None:
        assert not audit._GH_TRANSIENT.search(stderr), why


class TestTheRetryLoop:
    @staticmethod
    def _drive(results: list):
        """`_gh` against a scripted sequence of `subprocess.run` verdicts."""
        seq = iter(results)
        err = None
        out = None
        with (
            patch.object(subprocess, "run", side_effect=lambda *a, **k: next(seq)) as run,
            patch.object(audit.time, "sleep") as sleep,
        ):
            try:
                out = audit._gh("api", "repos/o/r/actions/runs/1/jobs")
            except SystemExit as exc:
                err = exc
        return run, sleep, err, out

    def test_a_transient_failure_is_retried_and_can_then_succeed(self) -> None:
        run, sleep, err, out = self._drive(
            [
                SimpleNamespace(returncode=1, stdout="", stderr="gh: Server Error (HTTP 502)"),
                SimpleNamespace(returncode=0, stdout="[]", stderr=""),
            ]
        )
        assert err is None
        assert out == "[]"
        assert run.call_count == 2
        assert sleep.call_count == 1

    def test_a_definitive_failure_reports_at_once(self) -> None:
        run, sleep, err, _ = self._drive(
            [SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")]
        )
        assert err is not None
        assert "404" in str(err)
        assert run.call_count == 1
        assert sleep.call_count == 0
        assert "attempts" not in str(err), "it did not retry, so it must not claim to have"

    def test_a_transient_failure_that_never_clears_is_BOUNDED(self) -> None:
        run, sleep, err, _ = self._drive(
            [SimpleNamespace(returncode=1, stdout="", stderr="gh: Server Error (HTTP 502)")]
            * (audit._GH_RETRIES + 5)
        )
        assert err is not None
        assert run.call_count == audit._GH_RETRIES + 1
        assert sleep.call_count == audit._GH_RETRIES
        assert f"after {audit._GH_RETRIES + 1} attempts" in str(err)

    def test_the_backoff_is_bounded_by_the_ceiling(self) -> None:
        """Mirrors `terrapod.http_retry`'s curve; the ceiling is what stops a
        long audit from sleeping for minutes on a sustained outage."""
        _run, sleep, _err, _ = self._drive(
            [SimpleNamespace(returncode=1, stdout="", stderr="gh: Server Error (HTTP 502)")]
            * (audit._GH_RETRIES + 5)
        )
        delays = [c.args[0] for c in sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0], delays
        assert all(d <= audit._GH_MAX_DELAY for d in delays)
