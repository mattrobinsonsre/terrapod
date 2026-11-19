"""The admission ledger (#2589 step 2).

What this guards is a MERGE rule, not a write: a second admission of the same
bytes under a new name must add a naming without re-deciding who vouched for the
artifact, while genuinely stronger evidence about those same bytes must be
taken. Those two pull in opposite directions, so each needs its own case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services import artifact_ledger as al


class TestCoordinate:
    def test_empty_parts_are_dropped(self) -> None:
        """A package with no version must not record `version: ""` -- an empty
        string reads as a version somebody chose."""
        c = al.coordinate("package", ecosystem="npm", name="x", version="", filename="x.tgz")
        assert c == {"kind": "package", "ecosystem": "npm", "name": "x", "filename": "x.tgz"}


class TestAttestationStrength:
    """Which evidence supersedes which.

    The ordering exists so a row admitted on a bare digest can be upgraded when
    a later pull verifies the publisher -- new evidence about the same bytes,
    not a different answer.
    """

    def test_publisher_beats_digest_beats_nothing(self) -> None:
        assert al._is_stronger("upstream-digest", "none-published")
        assert al._is_stronger("upstream-publisher", "upstream-digest")
        assert al._is_stronger("upstream-publisher", "none-published")

    def test_it_never_goes_backwards(self) -> None:
        assert not al._is_stronger("none-published", "upstream-digest")
        assert not al._is_stronger("upstream-digest", "upstream-publisher")

    def test_equal_is_not_stronger(self) -> None:
        """Or every re-pull would rewrite the row for no reason."""
        for level in al._STRENGTH:
            assert not al._is_stronger(level, level)

    @pytest.mark.parametrize("other", ["none-published", "upstream-digest", "upstream-publisher"])
    def test_an_operator_upload_is_off_the_scale_in_both_directions(self, other: str) -> None:
        """A named person vouching is a different KIND of claim from a
        cryptographic one, not a weaker or stronger one. It must neither upgrade
        a verified row nor be quietly overwritten by one -- an upload is the
        only attestation whose evidence cannot be re-checked later, so losing it
        loses the only record of who accepted the artifact.
        """
        assert not al._is_stronger("operator-upload", other)
        assert not al._is_stronger(other, "operator-upload")


@pytest.mark.asyncio
class TestRecordAdmission:
    async def test_a_new_digest_inserts_and_does_not_read_back(self) -> None:
        """The insert reports whether it took effect, so the common path costs
        one statement rather than a select-then-insert."""
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        await al.record_admission(
            db, digest="sha256:a", kind="package", attestation="upstream-digest"
        )

        assert db.execute.await_count == 1

    async def test_a_ledger_failure_never_propagates(self) -> None:
        """Admission must not fail because the ledger did. For an upload that is
        not a preference: gating on our own bookkeeping would make an operator's
        deliberate act fail for a reason unrelated to the artifact."""
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("the database is away"))

        await al.record_admission(
            db, digest="sha256:a", kind="package", attestation="operator-upload"
        )

    async def test_a_second_naming_is_added_and_the_attestor_is_left_alone(self) -> None:
        """Two names for one set of bytes is ONE row. The first admission is the
        one that happened; a later pull under a different name does not
        re-decide who vouched for it."""
        row = MagicMock()
        row.coordinates = [{"kind": "package", "name": "a"}]
        row.attestation = "upstream-publisher"
        row.attestor = {"publisher": "pypi/flask"}

        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(rowcount=0),  # the insert conflicted
                MagicMock(scalar_one_or_none=MagicMock(return_value=row)),
            ]
        )
        db.flush = AsyncMock()

        await al.record_admission(
            db,
            digest="sha256:a",
            kind="package",
            attestation="upstream-digest",
            attestor={"publisher": "somewhere-else"},
            coordinates={"kind": "package", "name": "b"},
        )

        assert row.coordinates == [
            {"kind": "package", "name": "a"},
            {"kind": "package", "name": "b"},
        ]
        assert row.attestation == "upstream-publisher"
        assert row.attestor == {"publisher": "pypi/flask"}

    async def test_a_duplicate_naming_is_not_recorded_twice(self) -> None:
        """Re-pulling the same artifact under the same name must not grow the
        list without bound."""
        row = MagicMock()
        row.coordinates = [{"kind": "package", "name": "a"}]
        row.attestation = "upstream-digest"

        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(rowcount=0),
                MagicMock(scalar_one_or_none=MagicMock(return_value=row)),
            ]
        )
        db.flush = AsyncMock()

        await al.record_admission(
            db,
            digest="sha256:a",
            kind="package",
            attestation="upstream-digest",
            coordinates={"kind": "package", "name": "a"},
        )

        assert row.coordinates == [{"kind": "package", "name": "a"}]
        assert db.flush.await_count == 0, "nothing changed, so nothing should be written"

    async def test_stronger_evidence_about_the_same_bytes_is_taken(self) -> None:
        """The upgrade case: admitted on a bare digest, and a later pull
        verified the publisher."""
        row = MagicMock()
        row.coordinates = []
        row.attestation = "upstream-digest"
        row.attestor = {}

        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(rowcount=0),
                MagicMock(scalar_one_or_none=MagicMock(return_value=row)),
            ]
        )
        db.flush = AsyncMock()

        await al.record_admission(
            db,
            digest="sha256:a",
            kind="package",
            attestation="upstream-publisher",
            attestor={"publisher": "pypi/flask"},
        )

        assert row.attestation == "upstream-publisher"
        assert row.attestor == {"publisher": "pypi/flask"}
        assert db.flush.await_count == 1
