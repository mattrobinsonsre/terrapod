"""Tests for the accepted-risk register review (#1255).

The whole job is a comparison between two lists, and every way it can break
produces a green run reporting nothing: a parser reading the wrong field, an id
that does not match because of case or a trailing slash, a scanner that failed
to run being indistinguishable from one that found nothing. None of that raises.

So these pin the classification itself — that a fixable entry is reported, that
an unfixed one is not, and that a scanner which did not run cannot cause an
entry to be called stale.
"""

from __future__ import annotations

import json
import pathlib

from conftest import register_review as rr

# ── register parsing ────────────────────────────────────────────────


def test_register_parsing_ignores_comments_and_blank_lines(tmp_path):
    """All three registers are mostly prose. A parser that took whole lines
    would compare reasoning paragraphs against advisory ids and match nothing,
    reporting a clean review forever."""
    f = tmp_path / ".trivyignore"
    f.write_text(
        "# CVE-2025-69720 — ncurses, discussed at length\n"
        "#\n"
        "# EXIT: Debian ships a fix.\n"
        "CVE-2025-69720\n"
        "\n"
        "GHSA-aaaa-bbbb-cccc  # trailing comment\n"
    )
    assert rr.read_register(f) == ["CVE-2025-69720", "GHSA-aaaa-bbbb-cccc"]


def test_a_missing_register_is_empty_not_fatal(tmp_path):
    """An emptied register is the goal, and deleting the file is a reasonable
    end state. It must not take the review down with it."""
    assert rr.read_register(tmp_path / "nope.txt") == []


# ── scanner parsing ─────────────────────────────────────────────────


def test_trivy_fix_version_is_carried_through():
    """The report has to say what to bump to. Reporting only that a fix exists
    leaves the reader doing the lookup the scanner already did."""
    found = rr.from_trivy(
        {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-69247",
                            "PkgName": "cryptography",
                            "InstalledVersion": "49.0.0",
                            "FixedVersion": "50.0.0",
                        }
                    ]
                }
            ]
        }
    )
    assert found["CVE-2026-69247"]["fixed"] == "50.0.0"
    assert found["CVE-2026-69247"]["package"] == "cryptography"


def test_pip_audit_unfixed_advisory_reports_no_fix():
    """pip-audit lists unfixed advisories too — unlike Trivy, which is run
    fix-only. Treating presence as fixability would report every entry that is
    correctly still accepted, which is the noise that gets a report muted."""
    found = rr.from_pip_audit(
        {
            "dependencies": [
                {
                    "name": "pyjwt",
                    "version": "2.13.0",
                    "vulns": [{"id": "PYSEC-2025-183", "fix_versions": []}],
                }
            ]
        }
    )
    assert found["PYSEC-2025-183"]["fixed"] == ""


def test_pip_audit_aliases_match_too():
    """Real pip-audit output carries `aliases`, and the same advisory has both a
    PYSEC id and a CVE. Which one is in the register depends on where whoever
    added it was reading. Matching only the primary id would call a live
    suppression stale — and stale entries are reported as safe to delete."""
    found = rr.from_pip_audit(
        {
            "dependencies": [
                {
                    "name": "pkg",
                    "version": "1.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2026-2463",
                            "aliases": ["CVE-2026-11111", "GHSA-aaaa-bbbb-cccc"],
                            "fix_versions": ["1.2.5"],
                        }
                    ],
                }
            ]
        }
    )
    assert found["PYSEC-2026-2463"]["fixed"] == "1.2.5"
    assert found["CVE-2026-11111"]["fixed"] == "1.2.5"
    assert found["GHSA-aaaa-bbbb-cccc"]["fixed"] == "1.2.5"


def test_npm_ids_come_out_of_via_urls():
    """npm keys by package, not advisory, so the GHSA only exists at the end of
    the advisory URL — the same path the CI gate walks."""
    found = rr.from_npm_audit(
        {
            "vulnerabilities": {
                "brace-expansion": {
                    "severity": "high",
                    "range": "2.0.0 - 2.1.3",
                    "fixAvailable": {"name": "brace-expansion", "version": "2.1.4"},
                    "via": [
                        {"url": "https://github.com/advisories/GHSA-rgw5-rvv9-x895"}
                    ],
                }
            }
        }
    )
    assert found["GHSA-rgw5-rvv9-x895"]["fixed"] == "brace-expansion@2.1.4"


def test_npm_fix_available_false_is_not_a_fix():
    found = rr.from_npm_audit(
        {
            "vulnerabilities": {
                "thing": {
                    "fixAvailable": False,
                    "via": [{"url": "https://x/advisories/GHSA-z"}],
                }
            }
        }
    )
    assert found["GHSA-z"]["fixed"] == ""


# ── classification ──────────────────────────────────────────────────


def test_entry_with_a_fix_is_fixable_and_one_without_is_holding():
    """The distinction the whole report rests on. Getting it backwards would
    either bury a real bump among entries doing their job, or nag weekly about
    something with nothing to bump to."""
    fixable, unmatched, holding = rr.classify(
        {"trivy": ["CVE-A"], "pip-audit": ["PYSEC-B"], "npm-audit": []},
        {
            "trivy": {"CVE-A": {"package": "p", "installed": "1", "fixed": "2"}},
            "pip-audit": {"PYSEC-B": {"package": "q", "installed": "1", "fixed": ""}},
        },
    )
    assert [r["id"] for r in fixable] == ["CVE-A"]
    assert [r["id"] for r in holding] == ["PYSEC-B"]
    assert unmatched == []


def test_entry_reported_by_nobody_is_stale():
    fixable, unmatched, holding = rr.classify(
        {"trivy": ["CVE-GONE"], "pip-audit": [], "npm-audit": []}, {"trivy": {}}
    )
    assert [r["id"] for r in unmatched] == ["CVE-GONE"]
    assert fixable == [] and holding == []


# ── end to end ──────────────────────────────────────────────────────


def _run(tmp_path, monkeypatch, scans: dict, registers: dict):
    scan_dir = tmp_path / "scans"
    scan_dir.mkdir(parents=True)
    for name, payload in scans.items():
        (scan_dir / name).write_text(json.dumps(payload))

    pentest = tmp_path / "pentest"
    (pentest / "trivy").mkdir(parents=True)
    paths = {}
    for scanner, body in registers.items():
        rel = rr.REGISTERS[scanner]
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        paths[scanner] = rel
    monkeypatch.chdir(tmp_path)

    out = tmp_path / "body.md"
    monkeypatch.setattr("sys.argv", ["register_review.py", str(scan_dir), str(out)])
    rc = rr.main()
    return rc, out.read_text()


def test_a_newly_fixable_entry_is_reported_with_its_bump(tmp_path, monkeypatch, capsys):
    """The case this exists for: an entry accepted because nothing could be
    done, which upstream has since fixed."""
    rc, body = _run(
        tmp_path,
        monkeypatch,
        {
            "trivy-api.json": {
                "Results": [
                    {
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": "CVE-2026-69247",
                                "PkgName": "cryptography",
                                "InstalledVersion": "49.0.0",
                                "FixedVersion": "50.0.0",
                            }
                        ]
                    }
                ]
            }
        },
        {"trivy": "# reasoning\nCVE-2026-69247\n"},
    )
    assert rc == 0
    assert "Fixable now" in body
    assert "CVE-2026-69247" in body and "50.0.0" in body
    assert "has_findings=true" in capsys.readouterr().out


def test_a_scanner_that_did_not_run_cannot_make_an_entry_look_stale(
    tmp_path, monkeypatch, capsys
):
    """The failure that would erode trust fastest. If a Trivy leg dies and its
    output never lands, every Trivy entry matches nothing — and telling someone
    to delete a live suppression on that basis is worse than saying nothing."""
    rc, body = _run(
        tmp_path,
        monkeypatch,
        {},  # no scanner output at all
        {"trivy": "CVE-2025-69720\n"},
    )
    assert rc == 0
    assert "CVE-2025-69720" not in body
    assert "Incomplete run" in body
    assert "has_findings=false" in capsys.readouterr().out


def test_an_empty_register_reports_nothing_to_do(tmp_path, monkeypatch, capsys):
    """The steady state we want. It must not raise an issue."""
    rc, _ = _run(
        tmp_path,
        monkeypatch,
        {"trivy-api.json": {"Results": []}},
        {"trivy": "# none\n"},
    )
    assert rc == 0
    assert "has_findings=false" in capsys.readouterr().out


def test_the_fingerprint_tracks_the_finding_set_not_the_run(
    tmp_path, monkeypatch, capsys
):
    """The fingerprint decides whether to re-notify. If it changed every run the
    issue would comment weekly and be muted; if it never changed, a newly
    fixable entry would be added silently to a body nobody re-reads."""
    scan = {
        "trivy-api.json": {
            "Results": [
                {
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-A",
                            "PkgName": "p",
                            "InstalledVersion": "1",
                            "FixedVersion": "2",
                        }
                    ]
                }
            ]
        }
    }
    _run(tmp_path / "a", monkeypatch, scan, {"trivy": "CVE-A\n"})
    first = capsys.readouterr().out

    _run(tmp_path / "b", monkeypatch, scan, {"trivy": "CVE-A\n"})
    same = capsys.readouterr().out
    assert _fp(first) == _fp(same), "identical finding sets must not re-notify"

    scan["trivy-api.json"]["Results"][0]["Vulnerabilities"].append(
        {
            "VulnerabilityID": "CVE-B",
            "PkgName": "q",
            "InstalledVersion": "1",
            "FixedVersion": "3",
        }
    )
    _run(tmp_path / "c", monkeypatch, scan, {"trivy": "CVE-A\nCVE-B\n"})
    changed = capsys.readouterr().out
    assert _fp(first) != _fp(changed), "a new fixable entry must re-notify"


def _fp(stdout: str) -> str:
    return next(
        ln.split("=", 1)[1]
        for ln in stdout.splitlines()
        if ln.startswith("fingerprint=")
    )


def test_the_real_registers_parse(tmp_path):
    """Guards the convention rather than the content: if a register ever grows a
    format the parser does not understand, the review silently reviews nothing."""
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in rr.REGISTERS.values():
        path = root / rel
        if not path.exists():
            continue
        for entry in rr.read_register(path):
            assert entry.startswith(("CVE-", "GHSA-", "PYSEC-", "OSV-")), (
                f"{rel}: {entry!r} does not look like an advisory id — either the "
                f"file grew a new format or a comment marker was missed"
            )
