"""Presenting the artifact ledger to a reviewer (#2589 step 2e).

The presentation decisions here are the feature, not decoration, and each one is
there because the obvious alternative destroys the thing the page exists to
build:

**"Verified" and "clean" are two different claims.** Conflating them loses the
reviewer who knows the difference -- the one we are trying to convince. So
verification state and findings are separate values that a caller renders as
separate indicators, never combined into one severity.

**Three states, never two.** `inventoried` / `findings` / `not-inventoried`,
and the third carries its REASON. A blank cell must never read as safe: "we did
not look" and "we looked and found nothing" are the two facts a reviewer most
needs told apart, and only one of them is reassuring.

**Every verdict is dated.** A clean result with no date is worthless; with the
database epoch and the tool versions that produced it, it is evidence.

**Only a verification FAILURE is critical.** A known CVE in a vendored
dependency is advisory by design; styling it as an incident makes the platform
look as though it does not understand its own output, and buries the one thing
that does mean something happened.
"""

from __future__ import annotations

from typing import Any

#: Supply-chain states a row can be in. Three, deliberately -- see the module
#: docstring on why a blank cell is not one of them.
INVENTORIED = "inventoried"
HAS_FINDINGS = "findings"
NOT_INVENTORIED = "not-inventoried"

#: Why an artifact has no inventory. A state without a reason is the blank cell
#: wearing a label.
REASON_NO_LEDGER = "not-admitted-through-the-ledger"
REASON_NOT_YET = "awaiting-inventory"

#: Verification states, kept SEPARATE from the states above. Only `failed`
#: earns critical styling, because only `failed` means something happened.
VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"

#: Which attestations count as a publisher or a person having vouched. Derived
#: from the attestation vocabulary rather than re-listed, so a new attestation
#: cannot default to looking verified.
_VOUCHED = frozenset({"upstream-digest", "upstream-publisher", "operator-upload"})


def verification_state(attestation: str | None) -> str:
    """How an artifact was admitted, as one of three states.

    `None` is `unverified` rather than `failed`: a row written by a build that
    recorded nothing is not evidence that verification was attempted and did not
    pass. The serve path treats it as a miss and re-verifies, which is the
    honest reading.
    """
    if attestation is None or attestation == "":
        return UNVERIFIED
    if attestation in _VOUCHED:
        return VERIFIED
    # `none-published` and anything a later release adds. Not `failed`: nothing
    # failed, there was simply nothing to check.
    return UNVERIFIED


def supply_chain(entry: Any | None) -> dict:
    """The supply-chain cell for one artifact, from its ledger row.

    `entry` is None when no ledger row exists -- an artifact admitted before the
    ledger did, or by a path that does not record. That is reported as its own
    reason rather than as an absence, because a reviewer reading a blank cell
    concludes the wrong thing.
    """
    if entry is None:
        return {
            "state": NOT_INVENTORIED,
            "reason": REASON_NO_LEDGER,
            "findings": 0,
            "critical": 0,
            "malicious": 0,
            "verdict": None,
        }

    if entry.sbom is None:
        return {
            "state": NOT_INVENTORIED,
            # Distinct from the above: this one IS admitted and simply has not
            # been inventoried yet, which a sweep will fix without anybody
            # doing anything.
            "reason": REASON_NOT_YET,
            "findings": 0,
            "critical": 0,
            "malicious": 0,
            "verdict": None,
        }

    matches = entry.matches or []
    critical = sum(1 for m in matches if (m or {}).get("severity") in ("CRITICAL", "HIGH"))
    malicious = sum(1 for m in matches if (m or {}).get("malicious"))
    return {
        "state": HAS_FINDINGS if matches else INVENTORIED,
        "reason": None,
        "findings": len(matches),
        "critical": critical,
        # Surfaced separately from the severity count: a malicious package is
        # not a bug in a dependency, it is the artifact being hostile, and it is
        # the one finding class that is NOT advisory by design.
        "malicious": malicious,
        # What makes a clean result evidence rather than an assertion. None when
        # nothing has matched yet, which the state above already says.
        "verdict": entry.verdict_provenance or None,
    }


def packages_in(sbom: dict | None) -> int:
    """How many real packages an SBOM names.

    A component without a `purl` is a file entry rather than a package, and the
    two are mixed in one list -- so a raw component count reads as many times
    more inventory than there is.
    """
    if not sbom:
        return 0
    return sum(1 for c in (sbom.get("components") or []) if isinstance(c, dict) and c.get("purl"))
