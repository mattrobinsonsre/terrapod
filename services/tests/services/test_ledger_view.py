"""Presenting the ledger to a reviewer (#2589 step 2e).

The presentation decisions ARE the feature here, and each has an obvious
alternative that destroys what the page exists to do. These assert the
distinctions rather than the rendering:

- "verified" and "clean" are different claims, so they are different values;
- three states, never two, and the third carries its reason;
- only a verification FAILURE is critical -- a known CVE in a vendored
  dependency is advisory by design, and styling it as an incident buries the
  one thing that means something happened.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from terrapod.services import ledger_view as lv


class TestVerificationState:
    @pytest.mark.parametrize(
        ("attestation", "expected"),
        [
            ("upstream-publisher", lv.VERIFIED),
            ("upstream-digest", lv.VERIFIED),
            ("operator-upload", lv.VERIFIED),
            ("none-published", lv.UNVERIFIED),
            (None, lv.UNVERIFIED),
            ("", lv.UNVERIFIED),
        ],
    )
    def test_states(self, attestation, expected) -> None:
        assert lv.verification_state(attestation) == expected

    def test_an_unrecorded_admission_is_unverified_not_failed(self) -> None:
        """A row written by a build that recorded nothing is not evidence that
        verification was ATTEMPTED and did not pass. Calling it `failed` would
        raise a false alarm on every legacy row -- and `failed` is the only
        state that earns critical styling, so the cost is the badge meaning
        nothing."""
        assert lv.verification_state(None) != lv.FAILED

    def test_an_operator_upload_is_verified_rather_than_a_gap(self) -> None:
        """A named person in the organisation vouching IS an attestation. The
        #2599 position: an upload is a different kind of claim from a signature,
        not a weaker one."""
        assert lv.verification_state("operator-upload") == lv.VERIFIED

    def test_an_unknown_attestation_does_not_default_to_verified(self) -> None:
        """A later release adding an attestation must not make it look vouched
        for by accident."""
        assert lv.verification_state("something-new") == lv.UNVERIFIED


def _entry(sbom=None, matches=None, verdict=None):
    return SimpleNamespace(sbom=sbom, matches=matches or [], verdict_provenance=verdict or {})


class TestSupplyChain:
    def test_no_ledger_row_has_its_own_reason(self) -> None:
        """A blank cell must never read as safe. An artifact admitted before the
        ledger existed is a different fact from one awaiting inventory, and a
        reviewer needs to be able to tell."""
        out = lv.supply_chain(None)
        assert out["state"] == lv.NOT_INVENTORIED
        assert out["reason"] == lv.REASON_NO_LEDGER

    def test_awaiting_inventory_is_distinguished_from_never_admitted(self) -> None:
        """One a sweep will fix on its own; the other never will."""
        out = lv.supply_chain(_entry(sbom=None))
        assert out["state"] == lv.NOT_INVENTORIED
        assert out["reason"] == lv.REASON_NOT_YET
        assert out["reason"] != lv.REASON_NO_LEDGER

    def test_not_inventoried_never_reports_zero_findings_as_a_verdict(self) -> None:
        """`findings: 0` with no verdict is the blank cell wearing a number --
        the single most misleading thing this page could show."""
        for entry in (None, _entry(sbom=None)):
            out = lv.supply_chain(entry)
            assert out["findings"] == 0
            assert out["verdict"] is None

    def test_a_clean_inventory_carries_its_verdict(self) -> None:
        """A clean result with no date is worthless; with the epoch that
        produced it, it is evidence."""
        out = lv.supply_chain(_entry(sbom={"components": []}, verdict={"db": {"version": 2}}))
        assert out["state"] == lv.INVENTORIED
        assert out["findings"] == 0
        assert out["verdict"] == {"db": {"version": 2}}

    def test_findings_are_counted_and_graded_separately(self) -> None:
        out = lv.supply_chain(
            _entry(
                sbom={"components": []},
                matches=[
                    {"severity": "CRITICAL"},
                    {"severity": "HIGH"},
                    {"severity": "LOW"},
                ],
                verdict={"x": 1},
            )
        )
        assert out["state"] == lv.HAS_FINDINGS
        assert out["findings"] == 3
        assert out["critical"] == 2

    def test_malicious_is_surfaced_apart_from_severity(self) -> None:
        """A malicious package is not a bug in a dependency -- it is the
        artifact being hostile, and the one finding class that is NOT advisory
        by design. Folding it into a severity count hides exactly that."""
        out = lv.supply_chain(
            _entry(
                sbom={"components": []},
                matches=[{"severity": "LOW", "malicious": True}],
                verdict={"x": 1},
            )
        )
        assert out["malicious"] == 1
        assert out["critical"] == 0

    def test_a_malformed_match_does_not_crash_the_listing(self) -> None:
        out = lv.supply_chain(_entry(sbom={"components": []}, matches=[None, {}], verdict={"x": 1}))
        assert out["findings"] == 2


class TestPackagesIn:
    def test_only_purl_bearing_components_count(self) -> None:
        """A wheel's component list is mostly file entries; a raw count reads as
        many times more inventory than there is."""
        sbom = {"components": [{"purl": "pkg:pypi/x@1"}, {"name": "/METADATA"}, "junk"]}
        assert lv.packages_in(sbom) == 1

    def test_no_sbom_is_zero(self) -> None:
        assert lv.packages_in(None) == 0
        assert lv.packages_in({}) == 0
