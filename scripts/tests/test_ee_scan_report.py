"""Tests for the EE scan report (#2154).

Its failure modes are all quiet. A reporter that drops a line reports fewer
images than are affected; one whose marker omits the fingerprint comments on
every run until someone mutes the job; one that treats an empty set as "closed"
loses an issue somebody is mid-rebuild on. None of them crashes.
"""

from __future__ import annotations

import json

import pytest
from conftest import ee_scan_report as rep


def _finding(tmp_path, line, arch, ids, kind="dependency"):
    d = tmp_path / f"eescan-{line}-{arch}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "finding.json").write_text(
        json.dumps(
            {
                "release": f"ee-{line}",
                "source": f"ee:{line} ({arch})",
                "kind": kind,
                "findings": [{"id": i, "package": "p", "severity": "HIGH"} for i in ids],
            }
        )
    )
    return tmp_path


def test_the_marker_carries_the_fingerprint_the_script_reports(tmp_path):
    """The workflow decides whether to re-notify by grepping the EXISTING body
    for `MARKER:<fingerprint>`. A marker without one can never match, so the
    issue would comment on every single run until somebody muted the job — noise
    rather than an error, which is why nobody would chase it."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    out = tmp_path / "body.md"
    digest = rep.fingerprint(rep.by_line(rep.load(tmp_path)))
    out.write_text(rep.render(rep.by_line(rep.load(tmp_path)), digest, {}))
    assert f"<!-- {rep.MARKER}:{digest} -->" in out.read_text()


def test_architectures_merge_into_one_finding_per_line(tmp_path):
    """A finding on both arches is one thing to fix, not two. Listing it twice
    would make the report read as worse than it is."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1", "CVE-2"])
    _finding(tmp_path, "2.16", "arm64", ["CVE-1"])
    lines = rep.by_line(rep.load(tmp_path))
    assert lines == {"2.16": {"CVE-1", "CVE-2"}}


def test_separate_lines_stay_separate(tmp_path):
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    _finding(tmp_path, "2.21", "amd64", ["CVE-9"])
    assert set(rep.by_line(rep.load(tmp_path))) == {"2.16", "2.21"}


def test_the_body_names_the_dispatch_without_enumerating_the_lines(tmp_path):
    """Supersedes an earlier test that required the dispatch PER LINE.

    The reason that test existed still holds in full: the scheduled build will
    not pick these up, because it rebuilds on a moved digest or version and a
    Debian fix upstream has not rebuilt moves neither — so the action has to be
    in the report or the loop is not closed.

    What changed is that enumerating the affected lines in a PUBLIC issue is the
    map the convention forbids, and that enumeration is why #2168 had to be
    withdrawn. The dispatch form stays; the advisory names the lines."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    _finding(tmp_path, "2.21", "arm64", ["CVE-2"])
    lines = rep.by_line(rep.load(tmp_path))
    body = rep.render(lines, rep.fingerprint(lines), {})
    assert "gh workflow run ee-build.yml" in body
    assert "-f force=true" in body
    assert "line=2.16" not in body and "line=2.21" not in body


def test_an_unchanged_set_keeps_its_fingerprint(tmp_path):
    """Whether to notify and what to display are different decisions. The
    fingerprint is over the SET, so re-running on the same findings must not
    produce a new one."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-2", "CVE-1"])
    first = rep.fingerprint(rep.by_line(rep.load(tmp_path)))
    _finding(tmp_path, "2.16", "arm64", ["CVE-1", "CVE-2"])
    assert rep.fingerprint(rep.by_line(rep.load(tmp_path))) == first


def test_a_changed_set_changes_the_fingerprint(tmp_path):
    """The positive control: without it the test above would pass against a
    constant."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    before = rep.fingerprint(rep.by_line(rep.load(tmp_path)))
    _finding(tmp_path, "2.16", "amd64", ["CVE-1", "CVE-3"])
    assert rep.fingerprint(rep.by_line(rep.load(tmp_path))) != before


def test_a_code_finding_is_refused_not_skipped(tmp_path):
    """It would mean the scan was pointed at something other than an image.
    Dropping it silently would hide that."""
    _finding(tmp_path, "2.16", "amd64", ["X"], kind="code")
    with pytest.raises(SystemExit, match="image findings only"):
        rep.load(tmp_path)


def test_no_findings_writes_an_empty_body_and_says_so(tmp_path, capsys):
    """Empty must mean "do not raise or refresh" — never "close". Someone may be
    mid-rebuild, and a list that empties because a scan leg was skipped must not
    read as done."""
    out = tmp_path / "body.md"
    rep.main.__globals__["sys"].argv = ["x", str(tmp_path / "nothing"), str(out)]
    rep.main()
    printed = capsys.readouterr().out
    assert "has_findings=false" in printed
    assert out.read_text() == ""


# ── the public/private split ─────────────────────────────────────────


def test_the_public_body_names_no_advisory_and_no_package(tmp_path):
    """The issue is PUBLIC. Naming an image alongside what is wrong with it is a
    map for anyone deciding where to look, and the detail belongs in the draft
    advisory. The first version of this reporter listed CVE ids per line and had
    to be withdrawn (#2168), so this is pinned rather than trusted to review."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-2025-47273", "GHSA-6v7p-g79w-8964"])
    lines = rep.by_line(rep.load(tmp_path))
    body = rep.render(lines, rep.fingerprint(lines), {})
    for leak in ("CVE-", "GHSA-", "setuptools", "urllib3", "msgpack"):
        assert leak not in body, f"the public body leaks {leak!r}"


def test_the_public_body_does_not_name_the_affected_lines(tmp_path):
    """Counts are allowed — "how many" is not a map. "which line" is."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    _finding(tmp_path, "2.21", "amd64", ["CVE-2"])
    lines = rep.by_line(rep.load(tmp_path))
    body = rep.render(lines, rep.fingerprint(lines), {})
    assert "2 ansible-core line(s)" in body
    # the dispatch is a placeholder, not an enumeration
    assert "line=<LINE>" in body
    assert "line=2.16" not in body and "line=2.21" not in body


def test_a_missing_advisory_url_is_stated_not_silently_omitted(tmp_path):
    """With no detail in the issue, a missing advisory means the detail is
    NOWHERE. Omitting the link quietly would leave a report that points at
    nothing and reads complete."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    lines = rep.by_line(rep.load(tmp_path))
    body = rep.render(lines, rep.fingerprint(lines), {})
    assert "No draft advisory URL was recorded" in body


def test_advisory_links_are_rendered_when_present(tmp_path):
    """The positive control: without it the test above would pass against a
    reporter that never renders a link at all."""
    _finding(tmp_path, "2.16", "amd64", ["CVE-1"])
    lines = rep.by_line(rep.load(tmp_path))
    body = rep.render(lines, rep.fingerprint(lines), {"ee-2.16": "https://example.test/ghsa-x"})
    assert "https://example.test/ghsa-x" in body
    assert "No draft advisory URL was recorded" not in body
