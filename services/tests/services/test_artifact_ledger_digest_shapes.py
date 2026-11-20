"""The ledger's key must survive every digest shape its callers hold.

Three findings from the beta.6 review, all in the same two lines of code:

* an npm digest is 95 characters and the column was 80, so every npm admission
  raised `22001` -- caught and logged by `record_admission`, by which point the
  transaction was already aborted and the `CachedPackageFile` row written
  moments earlier never committed. npm pull-through cached nothing at all.
* `""` is a valid primary key, and Go modules, Pulumi plugins and NuGet
  packages all pass one, so they collided on a single row.
* `normalise_digest` corrupted an SRI -- it splits on `:`, which an SRI has
  none of, so `sha512-<base64>` became `sha256:sha512-<base64>` with the
  base64 lowercased. That was the fix first proposed for the second finding,
  and it would have mangled the digests the first finding exists to store.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import ArtifactLedgerEntry
from terrapod.services import artifact_ledger

#: What npm publishes: `sha512-` plus 88 base64 characters.
NPM_SRI = "sha512-" + "AbC/9+xZ" * 11
#: Deliberately mixed-case, because base64 is case-sensitive and the bug that
#: prompted this test lowercased it.
assert len(NPM_SRI) == 95, "the fixture must be the real 95-character length"


class TestTheColumnHoldsEveryShape:
    def test_the_digest_column_is_wide_enough_for_an_sri(self) -> None:
        width = ArtifactLedgerEntry.__table__.columns["digest"].type.length
        assert width is not None and width >= len(NPM_SRI), (
            f"digest is varchar({width}) and an npm SRI is {len(NPM_SRI)} characters. "
            "At 80 every npm admission raised 22001 and took the cache row with it."
        )

    def test_it_is_not_narrower_than_the_other_digest_columns(self) -> None:
        """The 80 was an outlier, which is the shape of the mistake."""
        from terrapod.db.models import Base

        widths = {
            f"{m.class_.__name__}.{name}": col.type.length
            for m in Base.registry.mappers
            for name, col in m.class_.__table__.columns.items()
            if name.endswith("digest") and getattr(col.type, "length", None)
        }
        ledger = widths["ArtifactLedgerEntry.digest"]
        assert ledger >= max(widths.values()) or ledger >= 200, (
            f"the ledger's digest ({ledger}) is narrower than a sibling: {widths}"
        )


class TestNormaliseDigestLeavesAnSRIAlone:
    def test_an_sri_is_returned_verbatim(self) -> None:
        # Both halves matter: the algorithm must not be re-labelled and the
        # base64 must not be case-folded.
        assert artifact_ledger.normalise_digest(NPM_SRI) == NPM_SRI

    def test_a_bare_hex_shasum_is_prefixed_and_lowered(self) -> None:
        assert artifact_ledger.normalise_digest("ABC123def") == "sha256:abc123def"

    def test_an_already_prefixed_digest_is_lowered_not_prefixed_again(self) -> None:
        assert artifact_ledger.normalise_digest("SHA256:ABC") == "sha256:abc"

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_nothing_normalises_to_nothing(self, value: str | None) -> None:
        assert artifact_ledger.normalise_digest(value) == ""  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["", "ABC", "sha256:abc", NPM_SRI, "  sha512-Zz==  "])
    def test_it_is_idempotent(self, value: str) -> None:
        """A reader normalises a stored value that an admission already wrote."""
        once = artifact_ledger.normalise_digest(value)
        assert artifact_ledger.normalise_digest(once) == once


class TestAnArtifactWithNoDigestIsNotRecorded:
    """`""` is a valid primary key, which is why this needs refusing explicitly."""

    @pytest.mark.parametrize("digest", ["", "   ", None])
    async def test_it_writes_nothing(self, digest: str | None) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        await artifact_ledger.record_admission(
            db,
            digest=digest,  # type: ignore[arg-type]
            kind="package",
            attestation="none-published",
            coordinates={"ecosystem": "go", "name": "example.com/m"},
        )
        assert db.execute.await_count == 0, (
            "an artifact with no digest has no identity in a table keyed on the "
            "digest; recording one collides every such artifact onto a single row"
        )

    async def test_a_real_digest_still_writes(self) -> None:
        """The refusal must not swallow the case it exists beside."""
        db = MagicMock()
        result = MagicMock()
        result.rowcount = 1
        db.execute = AsyncMock(return_value=result)
        await artifact_ledger.record_admission(
            db,
            digest=NPM_SRI,
            kind="package",
            attestation="upstream-digest",
            coordinates={"ecosystem": "npm", "name": "lodash"},
        )
        assert db.execute.await_count == 1
        written = db.execute.await_args.args[0]
        # The SRI reaches the insert unchanged -- not re-labelled, not lowered.
        assert NPM_SRI in str(written.compile().params.values()) or NPM_SRI in str(written), (
            "the SRI must reach the insert verbatim"
        )
