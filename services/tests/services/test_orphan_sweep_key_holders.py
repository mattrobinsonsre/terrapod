"""`_KEY_HOLDERS` is the guard that stops the orphan sweep deleting a live object.

It is a LIST, and its own comment used to claim it was derived from the models --
"so a fourth table with a `storage_key` is covered the day it is added". It is
not, and the gap is not academic: six models carry a `storage_key` and three are
in the tuple.

So this is the gate that makes the list honest. Every model carrying a
`storage_key` is either checked by the sweep or named here with the reason it is
not, and a seventh model fails until somebody decides which. The decision matters
in one direction only -- a model the sweep forgets to check is one whose live
objects it can delete -- so the failure message says so.

Derived from the mapper registry rather than from a second list, because a second
list is the thing being replaced.
"""

import sqlalchemy as sa

from terrapod.db.models import Base, OCIBlob, OCIManifest, OrphanedObject
from terrapod.services.orphaned_objects import _KEY_HOLDERS, _still_referenced

#: Why a `storage_key`-carrying model is NOT re-checked before a delete. Each
#: entry is a decision, and the string is the reason it was taken -- an entry
#: whose reason is empty is not a decision, which `test_every_exemption_carries_a_reason`
#: refuses.
_NOT_CHECKED: dict[type, str] = {
    OrphanedObject: (
        "the sweep's own table. Including it would make every key it is about to "
        "delete report as still referenced, disabling the sweep completely."
    ),
    OCIManifest: (
        "no caller of record() names an OCI key, so one cannot reach the sweep "
        "today. Add both OCI tables to _KEY_HOLDERS BEFORE the first recorder "
        "that can, or the sweep deletes a live manifest."
    ),
    OCIBlob: (
        "as OCIManifest -- latent rather than live, and the same ordering applies: "
        "the tuple gains it before any code records an OCI key as orphaned."
    ),
}


def _models_with_a_storage_key() -> set[type]:
    """Every mapped class carrying a `storage_key` column, from the registry."""
    return {m.class_ for m in Base.registry.mappers if "storage_key" in m.class_.__table__.columns}


class TestTheKeyHolderListIsClosed:
    def test_every_storage_key_model_is_checked_or_exempt(self) -> None:
        found = _models_with_a_storage_key()
        # The floor: if the introspection stops finding models, this gate passes
        # on nothing, which is the failure it exists to prevent.
        assert len(found) >= 6, (
            f"only {len(found)} models carry a storage_key -- introspection broken?"
        )

        undecided = found - set(_KEY_HOLDERS) - set(_NOT_CHECKED)
        assert not undecided, (
            "a model carries a `storage_key` and the orphan sweep neither checks it "
            "nor records why not: "
            f"{sorted(c.__name__ for c in undecided)}. "
            "If an orphan record can ever name one of its keys, add it to "
            "_KEY_HOLDERS -- otherwise the sweep will delete a live object. If it "
            "cannot, add it to _NOT_CHECKED with the reason."
        )

    def test_no_model_is_both_checked_and_exempt(self) -> None:
        both = set(_KEY_HOLDERS) & set(_NOT_CHECKED)
        assert not both, f"contradictory: {sorted(c.__name__ for c in both)}"

    def test_every_exemption_carries_a_reason(self) -> None:
        for model, reason in _NOT_CHECKED.items():
            assert len(reason.strip()) >= 40, (
                f"{model.__name__}'s exemption has no real reason -- an entry here is a "
                "decision about when live objects may be deleted, so it says why"
            )

    def test_the_sweeps_own_table_is_never_a_key_holder(self) -> None:
        # Its own entry, asserted separately: this is the one addition that does
        # not merely weaken the guard but removes it, because every candidate key
        # is in `orphaned_objects` by definition.
        assert OrphanedObject not in _KEY_HOLDERS
        assert OrphanedObject in _NOT_CHECKED


class TestTheGuardQueriesEveryHolder:
    """The list being complete is worth nothing if `_still_referenced` skips one."""

    async def test_it_queries_each_key_holder_until_one_matches(self) -> None:
        asked: list[str] = []

        class _DB:
            async def scalar(self, stmt: sa.Select) -> int | None:
                # The FROM clause names the table being asked about; the compiled
                # statement would also carry the select list, which is a literal
                # here and says nothing about the table.
                asked.extend(t.name for t in stmt.get_final_froms())
                return None

        assert await _still_referenced(_DB(), "cache/thing") is False
        assert asked == [m.__tablename__ for m in _KEY_HOLDERS], (
            f"every key holder must be asked before a delete is allowed; asked {asked}"
        )

    async def test_it_short_circuits_on_the_first_match(self) -> None:
        asked: list[str] = []

        class _DB:
            async def scalar(self, stmt: sa.Select) -> int | None:
                asked.extend(t.name for t in stmt.get_final_froms())
                return 1

        assert await _still_referenced(_DB(), "cache/thing") is True
        assert len(asked) == 1, "a match must stop the scan rather than query the rest"

    async def test_an_empty_key_is_never_recorded_as_an_orphan(self) -> None:
        # A blank key would match nothing in any holder, so the sweep would treat
        # it as unreferenced and hand `""` to the object store.
        from terrapod.services import orphaned_objects

        class _DB:
            async def execute(self, stmt: object) -> None:  # pragma: no cover
                raise AssertionError("an empty key must not reach the database")

        await orphaned_objects.record(_DB(), "", reason="whatever")
