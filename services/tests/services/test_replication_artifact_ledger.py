"""Replication of the artifact ledger (#2589 step 2).

#2589's decision 4 is that the ledger replicates **completely, or it means
nothing**: a partially replicated evidence store is worse than none, because a
promoted follower would answer "we never served that artifact" when the record
was on the old leader. That is a *wrong* answer rather than an absent one, in
the one surface whose entire purpose is being trustworthy after an incident.

So this is the full per-class matrix the gate requires, and each row is written
against what losing it would actually cost rather than as a shape check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import ArtifactLedgerEntry
from terrapod.services import replication, replication_registry

LEDGER = replication_registry.ARTIFACT_LEDGER

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32

PUBLISHER = {
    "identity": "https://github.com/o/r/.github/workflows/release.yml@refs/heads/main",
    "issuer": "https://token.actions.githubusercontent.com",
    "predicate": "https://docs.pypi.org/attestations/publish/v1",
}


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _ids_db(ids):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = ids
    db.execute.return_value = result
    return db


def _entry(digest=DIGEST, **kw):
    now = datetime.now(UTC)
    base = {
        "digest": digest,
        "kind": "package",
        "coordinates": [{"ecosystem": "pypi", "name": "flask", "version": "3.0.0"}],
        "attestation": "upstream-publisher",
        "attestor": dict(PUBLISHER),
        "admitted_at": now,
        "admission": {"policy": "default", "mode": "observe"},
        "sbom": None,
        "matches": [],
        "verdict_provenance": {"syft": "1.2.3", "db_epoch": "2026-11-18"},
        "updated_at": now,
    }
    base.update(kw)
    return ArtifactLedgerEntry(**base)


class TestTheLedgerReplicates:
    @pytest.mark.replication_matrix("artifact_ledger", "backfill-from-empty")
    async def test_backfill_carries_the_attestor_and_the_admission(self):
        """A promoted node with an empty ledger cannot answer the question the
        ledger exists for, and a backfill is the only thing that fills it -- a
        deltas-only class has nothing to send for history that predates the
        peer.

        The attestor is asserted field by field rather than as "present",
        because it IS the evidence: a row that arrives saying
        `upstream-publisher` with an empty attestor claims a verification nobody
        can check.
        """
        admitted = datetime.now(UTC) - timedelta(days=3)
        db = _rows_db([_entry(admitted_at=admitted)])

        page = await replication.read_backfill(db, LEDGER)

        assert page[0]["digest"] == DIGEST
        assert page[0]["kind"] == "package"
        assert page[0]["attestation"] == "upstream-publisher"
        assert page[0]["attestor"] == PUBLISHER
        assert page[0]["admission"] == {"policy": "default", "mode": "observe"}
        assert page[0]["coordinates"][0]["name"] == "flask"
        # RFC3339 with a trailing Z, the house rule and what the far end parses.
        raw = page[0]["admitted_at"]
        assert raw.endswith("Z"), raw
        assert datetime.fromisoformat(raw.replace("Z", "+00:00")) == admitted

    @pytest.mark.replication_matrix("artifact_ledger", "delta-apply")
    async def test_an_inventory_arriving_later_reaches_the_peer(self):
        """The SBOM is written AFTER the row -- a ledger entry exists before
        Syft has run on it -- so the update is a delta of its own. A peer that
        applies the insert and misses this holds a ledger with no inventory,
        which reads as "we never looked" rather than as "it has not run yet".
        """
        db = AsyncMock()
        existing = _entry()
        db.scalar.return_value = existing
        sbom = {"artifacts": [{"name": "werkzeug", "version": "3.0.1"}]}

        await replication.apply_upsert(db, LEDGER, {"digest": DIGEST, "sbom": sbom})

        assert existing.sbom == sbom

    @pytest.mark.replication_matrix("artifact_ledger", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _entry()
        db.scalar.return_value = existing
        payload = replication.serialize_row(LEDGER, existing)

        await replication.apply_upsert(db, LEDGER, payload)
        await replication.apply_upsert(db, LEDGER, payload)

        assert existing.digest == DIGEST
        assert existing.attestation == "upstream-publisher"
        assert existing.attestor == PUBLISHER
        assert existing.matches == []

    @pytest.mark.replication_matrix("artifact_ledger", "delete")
    async def test_delete_applies(self):
        """A ledger row is deleted only when an operator purges history
        deliberately; it is not reaped with the artifact, because what executed
        last quarter stays askable after the cache has moved on. The delta still
        has to carry, or a purge on the leader leaves the follower holding
        records the operator removed.
        """
        db = AsyncMock()

        await replication.apply_delete(db, LEDGER, DIGEST)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("artifact_ledger", "backfill-converges-deletion")
    async def test_a_purged_entry_does_not_come_back_to_life(self):
        """The #1115 defect, on a table where it is worse than usual.

        A row an operator purged is one they decided should not be on the
        record. A backfill that resurrects it puts it back -- and because this
        table is the evidence store, that is a record appearing to be
        authoritative after having been deliberately removed.
        """
        db = _ids_db([(DIGEST,), (OTHER,)])

        removed = await replication.reconcile_deletions(db, LEDGER, {DIGEST})

        assert removed == [OTHER]
