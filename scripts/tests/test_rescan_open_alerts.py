"""Tests for rescan_open_alerts (#1375).

The re-scan used to report how many results were in the Semgrep SARIF, which
knows nothing about triage. Dismissing an alert is how a code finding is
accepted — the counterpart of `.trivyignore` for dependencies — so a release
whose findings had all been reviewed kept raising "findings that need
attention" on every scan, pointing at a code-scanning view that showed nothing.
Nothing a maintainer did could discharge it.

The behaviour that matters is therefore not "does it count", but **what happens
when it cannot count**: this must fail CLOSED, leaving the SARIF number in
place, because a spurious issue is a nuisance and a missed finding is not.
"""

from __future__ import annotations

import json

from conftest import open_alerts


def _finding(tmp_path, release: str, code_findings: int, name="f.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"release": release, "code_findings": code_findings, "findings": []}))
    return p


def test_dismissed_findings_stop_being_reported(tmp_path, monkeypatch):
    """The reported bug: 10 results in the SARIF, all dismissed, so 0 open."""
    f = _finding(tmp_path, "v1.5.0", 10)
    monkeypatch.setattr(open_alerts, "open_alert_count", lambda tag: 0)

    open_alerts.main.__globals__["sys"].argv = ["x", str(tmp_path), json.dumps(["v1.5.0"])]
    assert open_alerts.main() == 0

    assert json.loads(f.read_text())["code_findings"] == 0


def test_open_findings_are_still_reported(tmp_path, monkeypatch):
    """Triage must not silence a finding nobody has accepted."""
    f = _finding(tmp_path, "v1.5.0", 10)
    monkeypatch.setattr(open_alerts, "open_alert_count", lambda tag: 3)

    open_alerts.main.__globals__["sys"].argv = ["x", str(tmp_path), json.dumps(["v1.5.0"])]
    open_alerts.main()

    assert json.loads(f.read_text())["code_findings"] == 3


def test_an_unreachable_api_leaves_the_sarif_count_alone(tmp_path, monkeypatch):
    """Fail CLOSED. SARIF ingestion is asynchronous and the API can fail, so a
    query that returns nothing must never be read as 'no findings' — that would
    silently drop a real one."""
    f = _finding(tmp_path, "v1.5.0", 7)
    monkeypatch.setattr(open_alerts, "open_alert_count", lambda tag: None)

    open_alerts.main.__globals__["sys"].argv = ["x", str(tmp_path), json.dumps(["v1.5.0"])]
    open_alerts.main()

    assert json.loads(f.read_text())["code_findings"] == 7


def test_only_the_matching_release_is_rewritten(tmp_path, monkeypatch):
    """Each release is triaged separately; one release's dismissals must not
    silence another's findings."""
    a = _finding(tmp_path, "v1.5.0", 10, "a.json")
    b = _finding(tmp_path, "v1.4.2", 13, "b.json")
    monkeypatch.setattr(open_alerts, "open_alert_count", lambda tag: 0 if tag == "v1.5.0" else None)

    open_alerts.main.__globals__["sys"].argv = [
        "x",
        str(tmp_path),
        json.dumps(["v1.5.0", "v1.4.2"]),
    ]
    open_alerts.main()

    assert json.loads(a.read_text())["code_findings"] == 0
    assert json.loads(b.read_text())["code_findings"] == 13


def test_malformed_tags_argument_changes_nothing(tmp_path):
    f = _finding(tmp_path, "v1.5.0", 4)
    open_alerts.main.__globals__["sys"].argv = ["x", str(tmp_path), "not-json"]
    assert open_alerts.main() == 0
    assert json.loads(f.read_text())["code_findings"] == 4


class _Result:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_a_failed_api_call_returns_none_not_zero(monkeypatch):
    """The safety property, tested at the level that owns it.

    A 404 for a ref means the analysis has not been processed yet, which is NOT
    the same as "no findings". Returning 0 here would read as a clean release
    and silently drop real findings.
    """
    monkeypatch.setattr(open_alerts.subprocess, "run", lambda *a, **k: _Result(1, err="HTTP 404"))
    assert open_alerts.open_alert_count("v1.5.0") is None


def test_an_unparseable_count_returns_none(monkeypatch):
    monkeypatch.setattr(
        open_alerts.subprocess, "run", lambda *a, **k: _Result(0, out="not-a-number")
    )
    assert open_alerts.open_alert_count("v1.5.0") is None


def test_a_successful_call_returns_the_count(monkeypatch):
    monkeypatch.setattr(open_alerts.subprocess, "run", lambda *a, **k: _Result(0, out="4\n"))
    assert open_alerts.open_alert_count("v1.5.0") == 4


def test_the_query_asks_only_for_open_alerts_on_that_tag(monkeypatch):
    """If the ref or the state filter were wrong the count would be someone
    else's — the default branch's, or every alert regardless of triage."""
    seen = {}

    def _capture(cmd, *a, **k):
        seen["url"] = cmd[2]
        return _Result(0, out="0")

    monkeypatch.setattr(open_alerts.subprocess, "run", _capture)
    open_alerts.open_alert_count("v1.5.0")
    assert "refs/tags/v1.5.0" in seen["url"]
    assert "state=open" in seen["url"]
