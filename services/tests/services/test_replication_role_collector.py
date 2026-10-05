"""The collector's own rules, which a real database would not exercise (#1981).

The behavioural half lives in `tests/integration/` against real rows. These are
the decisions the collector makes before it ever reaches the database: which
classes it cares about, what it does when one identity appears twice in a batch,
and what happens when propagation itself fails.
"""

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import replication, replication_registry
from terrapod.services.replication_sync import (
    _note_identity_before_apply,
    _propagate_identity_changes,
)
from terrapod.services.role_change_propagation import IDENTITY_ROLE_CLASSES

pytestmark = pytest.mark.asyncio

PLATFORM = replication_registry.PLATFORM_ROLE_ASSIGNMENTS

#: A non-identity class with a COMPOSITE key. A single-key class cannot test
#: the class filter: `_note_identity_before_apply` also bails when the decoded
#: id has fewer than two parts, so dropping the filter still ignores it and the
#: test passes for the wrong reason. This one reaches the filter and nothing
#: else.
OTHER_COMPOSITE = replication.get("variable_set_workspaces")


def _event(spec, parts):
    return {
        "entity-class": spec.name,
        "entity-id": replication.encode_entity_id(spec, parts),
        "op": replication.UPSERT,
    }


class TestTheFilterCannotDriftFromTheRegistry:
    """`IDENTITY_ROLE_CLASSES` holds strings, and a rename would be silent.

    The filter is matched against `spec.name`. Rename a class in the registry
    and the set stops matching it — the collector then ignores every delta for
    that class and the fix stops working with nothing to show for it, which is
    the fail-open this whole issue is about. These key on the registry objects
    so the rename fails here instead.
    """

    def test_every_name_in_the_filter_is_a_registered_class(self):
        for name in IDENTITY_ROLE_CLASSES:
            assert replication.get(name) is not None, (
                f"{name!r} is in IDENTITY_ROLE_CLASSES but is not a registered "
                "replicated class — the filter can never match it"
            )

    def test_both_assignment_classes_are_in_the_filter(self):
        for spec in (
            replication_registry.ROLE_ASSIGNMENTS,
            replication_registry.PLATFORM_ROLE_ASSIGNMENTS,
        ):
            assert spec.name in IDENTITY_ROLE_CLASSES, (
                f"{spec.name!r} decides an identity's authorization but is not "
                "collected, so a replicated change to it never reaches sessions"
            )


class TestOnlyIdentityClassesAreCollected:
    async def test_an_unrelated_class_is_ignored(self):
        """Every class flows through this call; only two of them matter.

        Without the filter the collector would query two assignment tables for
        every replicated row of every class — a per-row cost on the hot path
        for an answer it would then discard.
        """
        assert OTHER_COMPOSITE is not None, "fixture class is no longer registered"
        assert len(OTHER_COMPOSITE.pk_attrs) >= 2, (
            "this fixture must have a composite key or it tests the arity check, "
            "not the class filter"
        )
        pending: dict[tuple[str, str], set[str]] = {}
        with patch("terrapod.services.replication_sync.identity_role_names", AsyncMock()) as names:
            await _note_identity_before_apply(
                AsyncMock(),
                _event(
                    OTHER_COMPOSITE,
                    [
                        "00000000-0000-0000-0000-000000000001",
                        "00000000-0000-0000-0000-000000000002",
                    ],
                ),
                pending,
            )
        assert pending == {}
        names.assert_not_awaited()

    async def test_an_unknown_class_is_ignored_rather_than_raising(self):
        """A newer peer may replicate a class this node does not know."""
        pending: dict[tuple[str, str], set[str]] = {}
        await _note_identity_before_apply(
            AsyncMock(), {"entity-class": "not_a_class", "entity-id": "x", "op": "upsert"}, pending
        )
        assert pending == {}


class TestOneIdentityIsRecordedOnce:
    async def test_the_first_note_wins(self):
        """Several events for one person in a batch compare against the state
        before the BATCH, which is the net change the session owner lives with.

        Re-reading on the second event would capture a half-applied set and
        compare the end of the batch against the middle of it.
        """
        pending: dict[tuple[str, str], set[str]] = {}
        event = _event(PLATFORM, ["local", "a@b.c", "admin"])

        with patch(
            "terrapod.services.replication_sync.identity_role_names",
            AsyncMock(side_effect=[{"admin"}, {"should-not-be-used"}]),
        ) as names:
            await _note_identity_before_apply(AsyncMock(), event, pending)
            await _note_identity_before_apply(AsyncMock(), event, pending)

        assert pending == {("local", "a@b.c"): {"admin"}}
        assert names.await_count == 1, "the second event must not re-read the set"


class TestPropagationCannotBreakTheStream:
    async def test_a_failure_is_logged_and_swallowed(self):
        """The row has already landed and committed.

        Letting a Redis blip escape here would turn a missed session revocation
        into a replication outage — the stream stops, the cursor holds, and
        every class stops converging. The absolute session ceiling is the
        backstop that makes swallowing the right trade.
        """
        pending = {("local", "a@b.c"): {"admin"}}
        with patch(
            "terrapod.services.replication_sync.identity_role_names",
            AsyncMock(side_effect=RuntimeError("redis is down")),
        ):
            await _propagate_identity_changes(AsyncMock(), pending)  # must not raise

    async def test_one_failure_does_not_skip_the_rest(self):
        """A per-identity failure must not abandon the identities after it."""
        pending = {
            ("local", "first@b.c"): {"admin"},
            ("local", "second@b.c"): {"admin"},
        }
        seen: list[str] = []

        async def _names(db, provider, email):
            seen.append(email)
            if email == "first@b.c":
                raise RuntimeError("redis is down")
            return set()

        with (
            patch("terrapod.services.replication_sync.identity_role_names", _names),
            patch(
                "terrapod.services.replication_sync.propagate_identity_role_change",
                AsyncMock(return_value="revoked"),
            ),
        ):
            await _propagate_identity_changes(AsyncMock(), pending)

        assert seen == ["first@b.c", "second@b.c"], (
            "the second identity was skipped because the first failed"
        )
