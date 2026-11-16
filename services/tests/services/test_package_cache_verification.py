"""`_parse_digest`: the one place four ecosystems' digest forms are normalised.

A pure function, so it belongs here rather than in the integration tier. What the
gate DOES with the result is a property of the real fetch path and is tested in
`tests/integration/test_package_cache_integration.py`.

The case worth the file: SRI's separator is a DASH. A parser that splits on `:`
alone sees no digest at all for every npm package, which is silent and total.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from terrapod.services.package_cache.substrate import (
    _DIGEST_REQUIRED,
    DigestMismatch,
    _parse_digest,
)

BODY = b"some artifact bytes" * 40


class TestTheFormsEachEcosystemPublishes:
    def test_colon_hex_as_pypi_and_galaxy_publish_it(self) -> None:
        want = hashlib.sha256(BODY).hexdigest()
        assert _parse_digest(f"sha256:{want}") == ("sha256", want)

    def test_sri_dash_base64_as_npm_publishes_it(self) -> None:
        """npm's `integrity`. The separator is a dash and the value base64."""
        raw = hashlib.sha512(BODY).digest()
        sri = "sha512-" + base64.b64encode(raw).decode()
        assert _parse_digest(sri) == ("sha512", raw.hex())

    def test_npms_legacy_sha1_shasum_fallback(self) -> None:
        want = hashlib.sha1(BODY).hexdigest()  # noqa: S324 - upstream's choice, not ours
        assert _parse_digest(f"sha1:{want}") == ("sha1", want)

    def test_uppercase_hex_and_mixed_case_algorithm_normalise(self) -> None:
        want = hashlib.sha256(BODY).hexdigest()
        assert _parse_digest(f"SHA256:{want.upper()}") == ("sha256", want)


class TestNothingToParse:
    @pytest.mark.parametrize("declared", ["", "   ", None])
    def test_absent_is_none_rather_than_an_error(self, declared) -> None:
        """Absence is the caller's decision to make -- `_DIGEST_REQUIRED` decides
        whether it is fatal, and that is deliberately not this function's call."""
        assert _parse_digest(declared) is None


class TestSomethingWeCannotRead:
    """Each of these must RAISE rather than return None.

    An unparseable digest is a reason to stop. Returning None would route it into
    the absent path, where a non-required ecosystem caches the artifact
    unverified -- so the two cases must stay distinguishable.
    """

    def test_not_hex(self) -> None:
        with pytest.raises(DigestMismatch, match="not hex"):
            _parse_digest("sha256:nothexatall")

    def test_not_base64(self) -> None:
        with pytest.raises(DigestMismatch, match="not base64"):
            _parse_digest("sha512-!!!not base64!!!")

    def test_no_recognised_separator(self) -> None:
        with pytest.raises(DigestMismatch, match="no form we recognise"):
            _parse_digest("deadbeef")

    def test_algorithm_we_do_not_compute(self) -> None:
        with pytest.raises(DigestMismatch, match="we do not compute"):
            _parse_digest("md5:" + "0" * 32)

    def test_right_algorithm_wrong_length(self) -> None:
        """A truncated digest must not be compared against a truncated computation."""
        with pytest.raises(DigestMismatch, match="not the 32"):
            _parse_digest("sha256:" + "ab" * 16)


class TestTheRequiredSet:
    def test_it_names_the_ecosystems_whose_upstream_always_publishes_one(self) -> None:
        """Asserted exactly, not as a subset.

        Adding an ecosystem here without wiring its digest breaks that ecosystem,
        and removing one silently stops requiring verification -- both directions
        matter, so a subset check would miss the one that loses a control.
        """
        assert _DIGEST_REQUIRED == frozenset({"pypi", "npm", "galaxy"})
