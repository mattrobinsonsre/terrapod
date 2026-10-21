"""A reported finding must be one a rebuild can actually clear (#2201).

`ignore-unfixed: true` makes Trivy report only findings with a fix published
*somewhere*. Two comments in the tree read that as "every reported finding is
actionable" — `ee_scan_report`'s docstring claimed such findings were "excluded
before they reach here" and the workflow's Trivy step claimed they "belong in
the register" — and **neither mechanism existed**. So the first EE scan raised a
public issue naming five findings "a rebuild would fix", for which a rebuild
would have fixed none, and five draft advisories each recommending a version the
image's own build cannot install.

The inference is sound for Debian packages, where `apt-get upgrade` stays inside
the distribution. It fails two ways these images hit immediately:

  * **a vendored copy** — `FixedVersion` names the standalone distribution while
    the finding is about a copy inside another one. Measured on the real
    published image: the finding said `wheel 0.45.1 -> 0.46.2` and the image's
    actual `wheel` was already **0.48.0**; the 0.45.1 was
    `setuptools/_vendor/wheel-0.45.1.dist-info`. `certifi` is the same family by
    a different door, declared in pip's own `_vendor/bom.cdx.json`, which is why
    Trivy gives it a `BOMRef` and no `PkgPath` at all.
  * **a package pinned to the image's line** — the image *is* the line, so a fix
    published only in a later one is unreachable by construction, and taking it
    would destroy the reason the line is built: the older lines exist precisely
    to reach a Python 2.6/2.7 target. "Fix 2.12 by installing 2.17" is not a fix,
    it is a different image.

Both directions are asserted throughout. A filter that excluded everything would
satisfy the complaint that produced it and be useless, so the in-line stable case
must still be reported, and the discriminating pair — one package, two paths — is
tested as a pair rather than as two independent cases.
"""

from __future__ import annotations

import json
import pathlib

from conftest import advisory, ee_scan_report, normalise

REPO = pathlib.Path(__file__).resolve().parents[2]


def _trivy(*vulns: dict) -> dict:
    """A Trivy image report, shaped as the real scanner emits it."""
    return {"Results": [{"Type": "python-pkg", "Class": "lang-pkgs", "Vulnerabilities": list(vulns)}]}


def _v(pkg: str, installed: str, fixed: str, *, path: str | None = None, bomref: str | None = None) -> dict:
    ident: dict = {"PURL": f"pkg:pypi/{pkg}@{installed}"}
    if bomref:
        ident["BOMRef"] = bomref
    out = {
        "VulnerabilityID": f"CVE-TEST-{pkg}",
        "PkgName": pkg,
        "InstalledVersion": installed,
        "FixedVersion": fixed,
        "Severity": "HIGH",
        "PkgIdentifier": ident,
    }
    if path is not None:
        out["PkgPath"] = path
    return out


SITE = "usr/local/lib/python3.10/site-packages"


class TestWhatARebuildCanClear:
    def test_a_fix_in_the_images_own_line_is_still_reported(self):
        """The direction that matters most: the filter must not swallow real work."""
        ok, why = normalise.reachable(
            {"package": "ansible-core", "fixed": "2.17.6, 2.16.13",
             "path": f"{SITE}/ansible_core-2.16.10.dist-info/METADATA"},
            "ee-2.16",
        )
        assert ok, why

    def test_a_fix_only_in_a_later_line_is_not(self):
        ok, why = normalise.reachable(
            {"package": "ansible-core", "fixed": "2.17.6, 2.16.13",
             "path": f"{SITE}/ansible_core-2.12.10.dist-info/METADATA"},
            "ee-2.12",
        )
        assert not ok
        assert "2.12" in why and "pinned" in why

    def test_a_fix_in_the_line_that_is_only_a_pre_release_is_not(self):
        """The platform's pre-release policy refuses these by default, so offering
        one as the fix would recommend something it would not install."""
        ok, why = normalise.reachable(
            {"package": "ansible-core", "fixed": "2.16.19rc1, 2.18.18rc1",
             "path": f"{SITE}/ansible_core-2.16.10.dist-info/METADATA"},
            "ee-2.16",
        )
        assert not ok
        assert "pre-release" in why

    def test_one_package_two_paths_is_the_whole_discrimination(self):
        """Asserted as a PAIR. Either case alone passes under a filter that keys on
        the package name, which is exactly the ledger this avoids."""
        real, _ = normalise.reachable(
            {"package": "wheel", "fixed": "0.46.2", "path": f"{SITE}/wheel-0.45.1.dist-info/METADATA"},
            "ee-2.12",
        )
        vendored, why = normalise.reachable(
            {"package": "wheel", "fixed": "0.46.2",
             "path": f"{SITE}/setuptools/_vendor/wheel-0.45.1.dist-info/METADATA"},
            "ee-2.12",
        )
        assert real, "the distribution the build installs must stay reportable"
        assert not vendored, "the copy inside setuptools cannot be upgraded by installing 0.46.2"
        assert "vendored" in why

    def test_an_sbom_declared_package_with_no_path_is_not(self):
        ok, why = normalise.reachable(
            {"package": "certifi", "fixed": "2026.10.19", "path": "", "from_sbom": True}, "ee-2.12"
        )
        assert not ok
        assert "SBOM" in why

    def test_no_path_and_no_sbom_marker_is_REPORTED_rather_than_guessed_away(self):
        """Fails open on purpose. A finding we cannot classify is one a maintainer
        should see; silently dropping it would make this filter the new way a real
        finding goes missing."""
        ok, why = normalise.reachable({"package": "cryptography", "fixed": "44.0.1", "path": ""}, "ee-2.12")
        assert ok, why


class TestTheClassificationRidesOnTrivysOwnFields:
    def test_from_trivy_carries_the_path_and_the_sbom_marker(self):
        out = normalise.from_trivy(
            _trivy(
                _v("jaraco.context", "5.3.0", "6.1.0", path=f"{SITE}/setuptools/_vendor/jaraco.context-5.3.0.dist-info/METADATA"),
                _v("certifi", "2026.6.17", "2026.10.19", bomref="pkg:pypi/certifi@2026.6.17"),
            ),
            "ee-2.12",
        )
        by_pkg = {f["package"]: f for f in out}
        assert "_vendor/" in by_pkg["jaraco.context"]["path"]
        assert by_pkg["jaraco.context"]["reachable"] is False
        assert by_pkg["certifi"]["path"] == ""
        assert by_pkg["certifi"]["from_sbom"] is True
        assert by_pkg["certifi"]["reachable"] is False

    def test_the_five_findings_that_produced_the_issue_yield_none(self):
        """The regression, with the exact shapes the real scan produced."""
        out = normalise.from_trivy(
            _trivy(
                _v("ansible-core", "2.12.10", "2.17.6, 2.16.13", path=f"{SITE}/ansible_core-2.12.10.dist-info/METADATA"),
                _v("ansible-core", "2.12.10", "2.16.19rc1, 2.18.18rc1, 2.19.11rc1, 2.20.7rc1, 2.21.1rc1",
                   path=f"{SITE}/ansible_core-2.12.10.dist-info/METADATA"),
                _v("certifi", "2026.6.17", "2026.10.19", bomref="pkg:pypi/certifi@2026.6.17"),
                _v("jaraco.context", "5.3.0", "6.1.0", path=f"{SITE}/setuptools/_vendor/jaraco.context-5.3.0.dist-info/METADATA"),
                _v("wheel", "0.45.1", "0.46.2", path=f"{SITE}/setuptools/_vendor/wheel-0.45.1.dist-info/METADATA"),
            ),
            "ee-2.12",
        )
        assert len(out) == 5, "every finding is still recorded, with its reason"
        assert [f for f in out if f["reachable"]] == [], "a rebuild clears none of them"
        assert all(f["unreachable"] for f in out), "and each says why"

    def test_the_dockerfile_still_pins_only_this(self):
        """`LINE_PINNED` is a package NAME, which is the one thing here that could
        rot. It is a fact about `Dockerfile.ee`, so read it from there: if a second
        package is ever pinned to the line, or this one stops being, the name is no
        longer the right subject and this fails instead of quietly misclassifying.
        """
        text = (REPO / "docker" / "Dockerfile.ee").read_text()
        pinned = [
            line.strip()
            for line in text.splitlines()
            if "pip install" in line and "==" in line
        ]
        assert len(pinned) == 1, f"expected exactly one pinned install, got {pinned}"
        assert f'"{normalise.LINE_PINNED}==' in pinned[0], (
            f"Dockerfile.ee pins {pinned[0]!r}, which is not {normalise.LINE_PINNED!r} -- "
            "`reachable` would classify the wrong package as line-pinned."
        )


class TestTheReportersDropWhatCannotBeCleared:
    def _report(self, tmp_path, findings):
        d = tmp_path / "eescan-2.12-amd64"
        d.mkdir(parents=True, exist_ok=True)
        (d / "finding.json").write_text(
            json.dumps({"release": "ee-2.12", "source": "ee:2.12 (amd64)",
                        "kind": "dependency", "findings": findings})
        )
        return tmp_path

    def test_the_issue_reports_nothing_when_nothing_is_clearable(self, tmp_path):
        root = self._report(tmp_path, [
            {"id": "CVE-A", "package": "ansible-core", "severity": "HIGH", "reachable": False,
             "unreachable": "fixed only outside the 2.12 line"},
        ])
        assert ee_scan_report.by_line(ee_scan_report.load(root)) == {}

    def test_a_clearable_one_alongside_them_is_still_reported(self, tmp_path):
        """Both directions: the filter must not hide a real finding that shares a
        report with unreachable ones."""
        root = self._report(tmp_path, [
            {"id": "CVE-A", "package": "ansible-core", "severity": "HIGH", "reachable": False, "unreachable": "x"},
            {"id": "CVE-B", "package": "cryptography", "severity": "HIGH", "reachable": True, "unreachable": ""},
        ])
        assert ee_scan_report.by_line(ee_scan_report.load(root)) == {"2.12": {"CVE-B"}}

    def test_a_finding_with_no_reachable_field_is_reported(self, tmp_path):
        """An absent signal must not read as "nothing to do" -- here of all places.
        A report written before the field existed, or by a leg that does not set
        it, keeps being reported."""
        root = self._report(tmp_path, [{"id": "CVE-C", "package": "p", "severity": "HIGH"}])
        assert ee_scan_report.by_line(ee_scan_report.load(root)) == {"2.12": {"CVE-C"}}

    def test_the_advisory_raises_nothing_for_a_line_with_nothing_clearable(self, tmp_path):
        """`load` keeps the release key with an empty list, and `main` skips a
        release with no findings (`if not findings: continue`) -- so the property
        to assert is that the line has nothing to record, not the dict's shape."""
        root = self._report(tmp_path, [
            {"id": "CVE-A", "package": "wheel", "severity": "HIGH", "reachable": False, "unreachable": "vendored"},
        ])
        loaded = advisory.load(root)
        assert loaded.get("ee-2.12") == [], loaded
        assert not any(loaded.values()), "no release has anything to put in an advisory"

    def test_the_advisory_still_records_a_clearable_one(self, tmp_path):
        root = self._report(tmp_path, [
            {"id": "CVE-A", "package": "wheel", "severity": "HIGH", "reachable": False, "unreachable": "vendored"},
            {"id": "CVE-B", "package": "cryptography", "severity": "HIGH", "reachable": True, "unreachable": ""},
        ])
        loaded = advisory.load(root)
        assert sorted(f["id"] for f in loaded["ee-2.12"]) == ["CVE-B"]
