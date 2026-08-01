"""Tests for the release re-scan scripts (#1212).

These three scripts decide what a security channel says, and their failure mode
is silence rather than a crash — a normaliser that reads the wrong field reports
zero findings and the run goes green. Both of the bugs pinned below shipped and
were caught by eye, not by CI, which is why they are tests now.

The privacy split is the other thing worth pinning: dependency findings may
reach the public issue, code findings must not, and the issue body must never
name what is wrong with which release.
"""

from __future__ import annotations

import json

import pytest
from conftest import advisory, normalise, report

# ── rescan_normalise ────────────────────────────────────────────────


def test_semgrep_count_reads_rule_level_severity():
    """Severity lives on the rule, not usually on the result.

    Reading `result["level"]` alone counted 0 while Semgrep reported 8 — the
    scan looked clean and the finding count silently vanished from the report.
    """
    sarif = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {"id": "r.error", "defaultConfiguration": {"level": "error"}},
                            {"id": "r.warn", "defaultConfiguration": {"level": "warning"}},
                        ]
                    }
                },
                "results": [
                    {"ruleId": "r.error"},
                    {"ruleId": "r.error"},
                    {"ruleId": "r.warn"},
                ],
            }
        ]
    }
    assert normalise.count_semgrep_sarif(sarif) == 2


def test_semgrep_count_prefers_result_level_when_present():
    sarif = {
        "runs": [
            {
                "tool": {
                    "driver": {"rules": [{"id": "r", "defaultConfiguration": {"level": "warning"}}]}
                },
                "results": [{"ruleId": "r", "level": "error"}],
            }
        ]
    }
    assert normalise.count_semgrep_sarif(sarif) == 1


@pytest.mark.parametrize("data", [None, {}, {"runs": []}, {"runs": [{"results": []}]}])
def test_semgrep_count_handles_empty_shapes(data):
    assert normalise.count_semgrep_sarif(data) == 0


def test_npm_audit_skips_unfixable():
    """Same ignore-unfixed policy as the artifact leg.

    A finding a patch release cannot action only pads the report with rows
    nobody can close.
    """
    data = {
        "vulnerabilities": {
            "nofix": {
                "severity": "high",
                "fixAvailable": False,
                "via": [{"url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}],
            }
        }
    }
    assert normalise.from_npm_audit(data, set()) == []


def test_npm_audit_reports_fixable_and_honours_allowlist():
    data = {
        "vulnerabilities": {
            "pkg": {
                "severity": "high",
                "range": "<1.2.3",
                "fixAvailable": {"name": "pkg", "version": "1.2.3"},
                "via": [
                    {"url": "https://github.com/advisories/GHSA-keep-keep-keep"},
                    {"url": "https://github.com/advisories/GHSA-drop-drop-drop"},
                ],
            }
        }
    }
    out = normalise.from_npm_audit(data, {"GHSA-drop-drop-drop"})
    assert [f["id"] for f in out] == ["GHSA-keep-keep-keep"]
    assert out[0]["fixed"] == "pkg@1.2.3"
    assert out[0]["ecosystem"] == "npm"


def test_npm_audit_marks_a_major_upgrade():
    data = {
        "vulnerabilities": {
            "pkg": {
                "severity": "critical",
                "fixAvailable": {"name": "pkg", "version": "9.0.0", "isSemVerMajor": True},
                "via": [{"url": "https://github.com/advisories/GHSA-1111-2222-3333"}],
            }
        }
    }
    assert normalise.from_npm_audit(data, set())[0]["fixed"] == "pkg@9.0.0 (major)"


def test_npm_audit_ignores_moderate_and_low():
    data = {
        "vulnerabilities": {
            "pkg": {
                "severity": "moderate",
                "fixAvailable": True,
                "via": [{"url": "https://github.com/advisories/GHSA-1111-2222-3333"}],
            }
        }
    }
    assert normalise.from_npm_audit(data, set()) == []


def test_pip_audit_skips_findings_with_no_fix_version():
    data = {
        "dependencies": [
            {"name": "a", "version": "1.0", "vulns": [{"id": "PYSEC-1", "fix_versions": []}]},
            {
                "name": "b",
                "version": "2.0",
                "vulns": [{"id": "PYSEC-2", "fix_versions": ["2.1", "3.0"]}],
            },
        ]
    }
    out = normalise.from_pip_audit(data)
    assert [f["id"] for f in out] == ["PYSEC-2"]
    assert out[0]["fixed"] == "2.1, 3.0"


def test_trivy_maps_ecosystems_and_falls_back_to_other():
    data = {
        "Results": [
            {
                "Type": "node-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-1",
                        "PkgName": "next",
                        "InstalledVersion": "1.0",
                        "FixedVersion": "1.1",
                        "Severity": "HIGH",
                    }
                ],
            },
            {
                "Type": "debian",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2",
                        "PkgName": "libexpat1",
                        "InstalledVersion": "2.7",
                        "FixedVersion": "2.8",
                        "Severity": "HIGH",
                    }
                ],
            },
        ]
    }
    out = normalise.from_trivy(data)
    assert [(f["id"], f["ecosystem"]) for f in out] == [("CVE-1", "npm"), ("CVE-2", "other")]


def test_read_treats_missing_empty_and_malformed_as_no_findings(tmp_path):
    """A scanner that produced nothing must not fail the run."""
    missing = tmp_path / "absent.json"
    empty = tmp_path / "empty.json"
    empty.write_text("")
    garbage = tmp_path / "bad.json"
    garbage.write_text("{not json")
    assert normalise._read(missing) is None
    assert normalise._read(empty) is None
    assert normalise._read(garbage) is None


def test_semgrep_findings_are_repo_relative():
    data = {
        "results": [
            {
                "check_id": "rule.a",
                "path": "/src/services/terrapod/api/app.py",
                "start": {"line": 42},
                "extra": {"severity": "ERROR"},
            },
            {
                "check_id": "rule.b",
                "path": "/src/x.py",
                "start": {"line": 1},
                "extra": {"severity": "WARNING"},
            },
        ]
    }
    out = normalise.from_semgrep(data)
    assert len(out) == 1
    assert out[0]["package"] == "services/terrapod/api/app.py:42"


# ── rescan_report (the public issue) ────────────────────────────────


def _write(directory, name, payload):
    path = directory / name
    path.write_text(json.dumps(payload))
    return path


def test_report_refuses_code_findings(tmp_path, capsys):
    """Code findings are refused, not merely skipped — this issue is public."""
    _write(
        tmp_path,
        "code.json",
        {
            "release": "v1.1.1",
            "kind": "code",
            "findings": [{"id": "rule.a", "package": "app.py:1"}],
        },
    )
    assert report.load(tmp_path) == []
    assert "REFUSING" in capsys.readouterr().err


def test_report_ignores_count_only_files(tmp_path):
    """A count-only file also has a `release`; accepting it would register the
    release as present-but-empty and mask its real findings."""
    _write(tmp_path, "count.json", {"release": "v1.1.1", "code_findings": 3})
    assert report.load(tmp_path) == []
    assert report.code_counts(tmp_path) == {"v1.1.1": 3}


def test_report_body_names_nothing(tmp_path):
    """The whole point: the public issue carries no advisory id, no package, no
    release name."""
    _write(
        tmp_path,
        "dep.json",
        {
            "release": "v1.1.1",
            "kind": "dependency",
            # A package name no prose would ever contain, so this asserts leakage
            # rather than tripping over an ordinary English word in the body.
            "findings": [{"id": "CVE-2026-64642", "package": "zlib-nonsense", "severity": "HIGH"}],
        },
    )
    out = tmp_path / "body.md"
    report.main.__globals__["sys"].argv = ["x", str(tmp_path), str(out)]
    assert report.main() == 0
    body = out.read_text()
    assert "CVE-2026-64642" not in body
    assert "zlib-nonsense" not in body
    assert "v1.1.1" not in body
    assert "security/advisories" in body


def test_report_fingerprint_ignores_which_image_saw_it(tmp_path):
    """The same advisory across five images is one thing to fix; letting the
    image list move the fingerprint would re-notify on noise."""
    finding = {"id": "CVE-1", "package": "curl", "severity": "HIGH"}
    one = [{"release": "v1.1.1", "source": "api", "findings": [finding]}]
    many = [
        {"release": "v1.1.1", "source": "api", "findings": [finding]},
        {"release": "v1.1.1", "source": "web", "findings": [finding]},
    ]
    assert report.fingerprint(one, {}) == report.fingerprint(many, {})


def test_report_fingerprint_moves_on_a_new_advisory():
    base = [{"release": "v1.1.1", "findings": [{"id": "CVE-1"}]}]
    more = [{"release": "v1.1.1", "findings": [{"id": "CVE-1"}, {"id": "CVE-2"}]}]
    assert report.fingerprint(base, {}) != report.fingerprint(more, {})


def test_report_fingerprint_moves_on_a_code_finding_count(tmp_path):
    assert report.fingerprint([], {"v1.1.1": 0}) != report.fingerprint([], {"v1.1.1": 3})


def test_report_says_nothing_when_there_is_nothing(tmp_path):
    out = tmp_path / "body.md"
    report.main.__globals__["sys"].argv = ["x", str(tmp_path), str(out)]
    assert report.main() == 0
    assert "No findings." in out.read_text()


def test_report_counts_code_findings_towards_notifying(tmp_path):
    """Code findings alone must still raise the issue, or a source-only finding
    is discovered by nobody."""
    _write(tmp_path, "count.json", {"release": "v1.1.1", "code_findings": 2})
    out = tmp_path / "body.md"
    report.main.__globals__["sys"].argv = ["x", str(tmp_path), str(out)]
    report.main()
    assert "No findings." not in out.read_text()


# ── rescan_advisory (the private draft) ─────────────────────────────


def test_advisory_dedupes_across_images(tmp_path):
    """The same CVE reported by five image scans is one row."""
    finding = {"id": "CVE-1", "package": "curl", "installed": "8.14", "fixed": "8.15"}
    _write(tmp_path, "api.json", {"release": "v1.1.1", "findings": [finding]})
    _write(tmp_path, "web.json", {"release": "v1.1.1", "findings": [finding]})
    _write(tmp_path, "other.json", {"release": "v1.2.0", "findings": [finding]})
    loaded = advisory.load(tmp_path)
    assert len(loaded["v1.1.1"]) == 1
    assert set(loaded) == {"v1.1.1", "v1.2.0"}


def test_advisory_description_carries_the_marker_and_the_rows():
    body = advisory.describe(
        "v1.1.1",
        [
            {
                "id": "CVE-1",
                "package": "curl",
                "installed": "8.14",
                "fixed": "8.15",
                "severity": "HIGH",
            }
        ],
        code_count=3,
    )
    assert f"<!-- {advisory.MARKER}:v1.1.1 -->" in body
    assert "`curl`" in body and "CVE-1" in body and "**8.15**" in body
    assert "3 code finding(s)" in body


def test_advisory_vulnerabilities_are_one_entry_per_package():
    findings = [
        {"package": "curl", "installed": "8.14", "fixed": "8.15", "ecosystem": "other"},
        {"package": "curl", "installed": "8.14", "fixed": "8.15", "ecosystem": "other"},
        {"package": "next", "installed": "16.2.10", "fixed": "16.2.11", "ecosystem": "npm"},
    ]
    out = advisory.vulnerabilities(findings)
    assert [v["package"]["name"] for v in out] == ["curl", "next"]
    assert out[1]["package"]["ecosystem"] == "npm"


def test_advisory_needs_no_token_when_there_is_nothing_to_record(tmp_path, monkeypatch):
    """A clean scan must not fail for want of a credential it never needed."""
    monkeypatch.delenv("ADVISORY_TOKEN", raising=False)
    urls = tmp_path / "urls.json"
    advisory.main.__globals__["sys"].argv = ["x", str(tmp_path), str(urls)]
    assert advisory.main() == 0
    assert json.loads(urls.read_text()) == {}


def test_advisory_fails_loudly_when_findings_exist_but_the_token_does_not(tmp_path, monkeypatch):
    """A security channel that degrades quietly is worse than one that is
    obviously broken."""
    _write(
        tmp_path,
        "dep.json",
        {
            "release": "v1.1.1",
            "findings": [{"id": "CVE-1", "package": "curl"}],
        },
    )
    monkeypatch.setenv("ADVISORY_TOKEN", "  ")
    urls = tmp_path / "urls.json"
    advisory.main.__globals__["sys"].argv = ["x", str(tmp_path), str(urls)]
    with pytest.raises(SystemExit) as exc:
        advisory.main()
    assert "ADVISORY_TOKEN" in str(exc.value)


def test_advisory_never_publishes():
    """Publishing is irreversible and makes an unfixed vulnerability public, so
    it stays a deliberate human act. A scheduled job holding a token that *can*
    publish must not be one keystroke from doing it by accident."""
    source = (advisory.__file__ and open(advisory.__file__).read()) or ""
    assert '"published"' not in source
    assert "'published'" not in source
    assert "state=published" not in source
