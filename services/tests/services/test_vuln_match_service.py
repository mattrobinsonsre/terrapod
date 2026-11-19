"""Matching a stored SBOM against the vulnerability database (#2589 2c, 2d).

Syft generates and Trivy consumes, which is why there is no third tool and why
the content check (2d) is not a separate pass: Trivy reports a known-malicious
package as a vulnerability, so it arrives in the same report as everything else.

The property guarded throughout is the difference between **no findings** and
**no match** -- an empty list is a reassuring claim, and only one of the two
establishes it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import vuln_match_service as vm


class TestFindings:
    def test_results_from_every_target_are_flattened(self) -> None:
        report = {
            "Results": [
                {"Vulnerabilities": [{"VulnerabilityID": "CVE-1", "PkgName": "a"}]},
                {"Vulnerabilities": [{"VulnerabilityID": "CVE-2", "PkgName": "b"}]},
                {"Vulnerabilities": None},
                {},
            ]
        }
        out = vm._findings(report)
        assert [f["id"] for f in out] == ["CVE-1", "CVE-2"]

    def test_a_malicious_package_is_flagged(self) -> None:
        """2d, and it comes free with 2c: a package flagged malicious is not a
        bug in a dependency, it is the artifact being hostile."""
        report = {
            "Results": [
                {
                    "Vulnerabilities": [
                        {"VulnerabilityID": "MAL-1", "PkgName": "evil", "Type": "malicious"},
                        {
                            "VulnerabilityID": "MAL-2",
                            "PkgName": "evil2",
                            "Title": "Malicious package detected",
                        },
                        {"VulnerabilityID": "CVE-9", "PkgName": "ordinary"},
                    ]
                }
            ]
        }
        out = vm._findings(report)
        assert [f["malicious"] for f in out] == [True, True, False]

    def test_severity_is_normalised(self) -> None:
        out = vm._findings({"Results": [{"Vulnerabilities": [{"Severity": "critical"}]}]})
        assert out[0]["severity"] == "CRITICAL"

    def test_an_absent_fix_is_none_not_empty_string(self) -> None:
        """`fixed: ""` would read as a version somebody could upgrade to."""
        out = vm._findings({"Results": [{"Vulnerabilities": [{"FixedVersion": ""}]}]})
        assert out[0]["fixed"] is None

    def test_a_non_dict_entry_is_skipped_rather_than_crashing(self) -> None:
        out = vm._findings(
            {"Results": [{"Vulnerabilities": ["garbage", {"VulnerabilityID": "CVE-1"}]}]}
        )
        assert [f["id"] for f in out] == ["CVE-1"]


@pytest.mark.asyncio
class TestDbEpoch:
    """The epoch comes from `trivy version`, NOT from the scan report.

    Measured: a `trivy sbom` report carries no `Metadata` key at all, so reading
    the epoch there yields `{}` every time -- and the re-match trigger silently
    degrades to "did the matcher version change", which no new CVE moves.
    """

    async def test_the_epoch_is_parsed_from_the_version_command(self) -> None:
        payload = (
            b'{"Version":"0.76.2","VulnerabilityDB":{"Version":2,'
            b'"UpdatedAt":"2026-11-19T07:06:49Z","NextUpdate":"2026-11-20T07:06:49Z",'
            b'"DownloadedAt":"2026-11-19T12:48:37Z"}}'
        )
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(stdout=payload))
        ):
            out = await vm._db_epoch("/x/trivy")

        assert out == {
            "version": 2,
            "updated_at": "2026-11-19T07:06:49Z",
            "next_update": "2026-11-20T07:06:49Z",
        }

    async def test_a_failure_is_an_empty_epoch_not_an_invented_one(self) -> None:
        """A fabricated epoch would make a stale finding look current."""
        for proc in (_Proc(rc=1), _Proc(stdout=b"not json"), _Proc(stdout=b"[]")):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
                assert await vm._db_epoch("/x/trivy") == {}

    async def test_trivy_missing_is_an_empty_epoch(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("gone")):
            assert await vm._db_epoch("/x/trivy") == {}


class TestProvenance:
    def test_the_binarys_own_version_wins_over_the_configured_pin(self) -> None:
        """An operator-uploaded trivy can differ from values.yaml, and the
        provenance must say what actually ran."""
        p = vm._provenance({"Trivy": {"Version": "9.9.9"}}, {})
        assert p["matcher_version"] == "9.9.9"

    def test_it_falls_back_to_the_pin_when_the_report_is_silent(self) -> None:
        p = vm._provenance({}, {})
        assert p["matcher_version"]

    def test_provenance_names_both_tools_a_time_and_the_epoch(self) -> None:
        """Without this, "no findings" is undated and unattributable."""
        p = vm._provenance({}, {"version": 2})
        assert p["matcher"] == "trivy"
        assert p["sbom_generator"] == "syft"
        assert p["sbom_generator_version"]
        assert p["db"] == {"version": 2}
        assert p["matched_at"]


class _Proc:
    def __init__(self, stdout=b"{}", rc=0):
        self._o, self.returncode = stdout, rc

    async def communicate(self):
        return self._o, b""

    def kill(self):
        pass

    async def wait(self):
        pass


@pytest.mark.asyncio
class TestMatch:
    async def test_no_trivy_is_none(self, tmp_path) -> None:
        with patch.object(vm, "trivy_binary", new=AsyncMock(return_value=None)):
            assert await vm.match({"components": []}) is None

    async def test_a_nonzero_exit_is_none_not_an_empty_finding_list(self, tmp_path) -> None:
        """The commonest cause is the vulnerability database being unreachable.
        Returning `[]` would record "we matched and found nothing" for an
        artifact nobody matched -- the single most misleading outcome available.
        """
        with patch.object(vm, "trivy_binary", new=AsyncMock(return_value="/x/trivy")):
            with patch("terrapod.services.api_tool.tool_dir", return_value=tmp_path):
                with patch(
                    "asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(rc=2))
                ):
                    assert await vm.match({"components": []}) is None

    async def test_output_that_is_not_json_is_none(self, tmp_path) -> None:
        with patch.object(vm, "trivy_binary", new=AsyncMock(return_value="/x/trivy")):
            with patch("terrapod.services.api_tool.tool_dir", return_value=tmp_path):
                with patch(
                    "asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=_Proc(stdout=b"not json")),
                ):
                    assert await vm.match({"components": []}) is None

    async def test_a_clean_report_is_an_empty_list_WITH_provenance(self, tmp_path) -> None:
        """The distinction the whole module turns on: this one DID match, so an
        empty list is a verdict and carries the epoch that produced it."""
        report = b'{"Results": [], "Trivy": {"Version": "0.76.2"}}'
        with patch.object(vm, "trivy_binary", new=AsyncMock(return_value="/x/trivy")):
            with patch("terrapod.services.api_tool.tool_dir", return_value=tmp_path):
                with patch(
                    "asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=_Proc(stdout=report)),
                ):
                    # The epoch comes from `trivy version`, not the report --
                    # a `trivy sbom` report carries no Metadata at all.
                    with patch.object(vm, "_db_epoch", new=AsyncMock(return_value={"version": 2})):
                        out = await vm.match({"components": []})

        assert out is not None
        matches, provenance = out
        assert matches == []
        assert provenance["db"] == {"version": 2}
        assert provenance["matcher_version"] == "0.76.2"

    async def test_the_sbom_temp_file_is_cleaned_up(self, tmp_path) -> None:
        with patch.object(vm, "trivy_binary", new=AsyncMock(return_value="/x/trivy")):
            with patch("terrapod.services.api_tool.tool_dir", return_value=tmp_path):
                with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc())):
                    await vm.match({"components": []})

        leftovers = (
            list((tmp_path / "match").glob("sbom.*")) if (tmp_path / "match").exists() else []
        )
        assert leftovers == [], leftovers
