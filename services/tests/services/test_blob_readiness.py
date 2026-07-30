"""Blob readiness: proving the object store is there (#1147).

The failure this exists to catch is **rows present, blobs absent** — a promoted
node that believes it has four hundred workspaces and cannot serve one. It looks
entirely healthy until somebody queues a run.

Most of these tests are about the check being *honest* rather than about it
working. A presence check is easy; a presence check that cannot be misread as more
than it is takes care. Specifically:

- a sample must never be reportable as a clean estate;
- "I could not look" must be distinguishable from "I looked and all is well";
- the thing that should stop a failover must be named, not left for the reader to
  derive from a list of classes.

The last one matters most. An operator reads this while deciding whether to move
DNS, and a readout that requires interpretation at that moment is a readout that
gets interpreted wrong.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import blob_readiness


class FakeStore:
    """An object store where a named set of keys is absent."""

    def __init__(self, missing: set[str] | None = None, raises: bool = False):
        self._missing = missing or set()
        self._raises = raises
        self.checked: list[str] = []

    async def exists(self, key: str) -> bool:
        self.checked.append(key)
        if self._raises:
            raise RuntimeError("store unreachable")
        return key not in self._missing


def _class(name="state", tier=blob_readiness.IRREPLACEABLE, **kw):
    return blob_readiness.ClassReadiness(name=name, tier=tier, **kw)


class TestTheHeadlineCannotBeMisread:
    """A clean sample is not a clean estate, and the result has to say so."""

    def test_a_clean_sample_is_healthy_but_not_complete(self):
        result = _class(total_rows=40_000, checked=25, missing=0, complete=False)

        assert result.healthy is True
        assert result.complete is False, (
            "25 of 40,000 checked must never report as complete — that is exactly "
            "the false confidence this check exists to remove"
        )

    def test_healthy_is_deliberately_not_called_ready(self):
        """`healthy` is the weak claim: nothing missing AMONG THOSE CHECKED.
        Naming it `ready` would invite reading a sample as a verdict."""
        assert not hasattr(_class(), "ready")

    def test_a_store_error_is_not_healthy(self):
        """'I could not look' must never read as 'all is well'. This is the
        distinction a boolean alone would lose."""
        result = _class(checked=0, missing=0, error="store unreachable")

        assert result.healthy is False

    def test_totals_count_only_what_was_checked(self):
        readiness = blob_readiness.BlobReadiness(
            classes=[_class(missing=3), _class(name="config", missing=2)]
        )

        assert readiness.missing_total == 5


class TestWhatShouldStopAFailover:
    """Named rather than derived. An operator reads this while deciding whether
    to move DNS; a readout needing interpretation gets interpreted wrong."""

    def test_missing_irreplaceable_objects_are_called_out(self):
        readiness = blob_readiness.BlobReadiness(
            classes=[
                _class(name="state", missing=4),
                _class(name="logs", tier=blob_readiness.HISTORY, missing=900),
            ]
        )

        assert readiness.irreplaceable_missing == ["state"]

    def test_lost_history_does_not_raise_the_alarm(self):
        """Nine hundred missing logs is a real gap and not a reason to abort a
        failover. Conflating the two makes the signal useless."""
        readiness = blob_readiness.BlobReadiness(
            classes=[_class(name="logs", tier=blob_readiness.HISTORY, missing=900)]
        )

        assert readiness.irreplaceable_missing == []
        assert readiness.missing_total == 900

    def test_a_re_derivable_class_is_its_own_tier(self):
        """Caches re-warm themselves — unless the node is sealed, when they are
        as fatal as state. The tier keeps that a deployment decision rather than
        being hard-coded into the alarm."""
        readiness = blob_readiness.BlobReadiness(
            classes=[_class(name="cache", tier=blob_readiness.REDERIVABLE, missing=50)]
        )

        assert readiness.irreplaceable_missing == []


class TestPresenceChecking:
    async def test_missing_keys_are_found(self):
        store = FakeStore(missing={"state/ws-1/sv-2.tfstate"})

        missing, examples = await blob_readiness._check_keys(
            store, ["state/ws-1/sv-1.tfstate", "state/ws-1/sv-2.tfstate"]
        )

        assert missing == 1
        assert examples == ["state/ws-1/sv-2.tfstate"]

    async def test_a_failing_check_counts_as_missing_rather_than_raising(self):
        """One unreadable object must not lose the other nine hundred results —
        the caller wants a readout, not an exception."""
        store = FakeStore(raises=True)

        missing, _ = await blob_readiness._check_keys(store, ["a", "b", "c"])

        assert missing == 3

    async def test_examples_are_capped(self):
        """A wholly absent class would otherwise return a wall of keys nobody
        reads."""
        wanted = [f"state/ws/{i}.tfstate" for i in range(50)]
        store = FakeStore(missing=set(wanted))

        missing, examples = await blob_readiness._check_keys(store, wanted)

        assert missing == 50
        assert len(examples) == 5

    async def test_every_key_is_checked_exactly_once(self):
        store = FakeStore()
        wanted = [f"k{i}" for i in range(30)]

        await blob_readiness._check_keys(store, wanted)

        assert sorted(store.checked) == sorted(wanted)

    async def test_concurrency_is_bounded(self):
        """A readiness check must not look like a load test against the store."""
        in_flight = 0
        peak = 0

        class Counting(FakeStore):
            async def exists(self, key: str) -> bool:
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    import asyncio

                    await asyncio.sleep(0)
                    return True
                finally:
                    in_flight -= 1

        await blob_readiness._check_keys(Counting(), [f"k{i}" for i in range(100)])

        assert peak <= blob_readiness._CONCURRENCY


class TestTheClassesCover_1114sIrreplaceableTier:
    """#1114 lists what is permanently lost if it is absent. Each entry is here
    for a reason worth stating, so the list cannot be trimmed casually."""

    def test_the_irreplaceable_classes_are_registered(self):
        registered = {name for name, _, _ in blob_readiness.RESOLVERS}

        assert registered == {
            "state",
            "state_index",
            "configuration_versions",
            "registry_modules",
            "registry_providers",
        }

    def test_all_registered_classes_are_irreplaceable(self):
        """History and re-derivable classes are deliberately not checked yet:
        the point of going first was the tier where absence is permanent."""
        assert all(tier == blob_readiness.IRREPLACEABLE for _, tier, _ in blob_readiness.RESOLVERS)

    def test_state_is_checked_first(self):
        """Order is the order an operator reads, and the order a copier should
        use — irreplaceable first, state before everything."""
        assert blob_readiness.RESOLVERS[0][0] == "state"


class TestResolvers:
    """Each resolver turns rows into the keys they imply. The interesting part is
    what each one is careful to include."""

    def _db(self, rows, count=0):
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = rows
        result.scalar.return_value = count
        db.execute.return_value = result
        return db

    async def test_state_resolves_every_version_not_just_the_latest(self):
        """Rollback is a shipped feature, so a node holding only HEAD has
        silently lost rollback depth — and looks healthy doing it."""
        db = self._db([("ws-1", "sv-1"), ("ws-1", "sv-2")], count=2)

        total, resolved = await blob_readiness._resolve_state(db, None)

        assert total == 2
        assert resolved == ["state/ws-1/sv-1.tfstate", "state/ws-1/sv-2.tfstate"]

    async def test_a_provider_platform_also_pulls_its_signed_manifest(self):
        """A present zip with an absent SHA256SUMS still fails `terraform init`,
        so checking only the binary would report a working registry that is not."""
        db = self._db([("default", "aws", "1.0.0", "linux", "amd64")], count=1)

        _, resolved = await blob_readiness._resolve_provider_binaries(db, None)

        assert resolved == [
            "registry/providers/default/aws/1.0.0/aws_1.0.0_linux_amd64.zip",
            "registry/providers/default/aws/1.0.0/SHA256SUMS",
            "registry/providers/default/aws/1.0.0/SHA256SUMS.sig",
        ]

    async def test_config_versions_resolve_to_their_tarball(self):
        """The sharpest omission in the store: a VCS workspace can refetch, a
        CLI-uploaded or catalog-provisioned one cannot — this is the only copy."""
        db = self._db([("ws-1", "cv-1")], count=1)

        _, resolved = await blob_readiness._resolve_config_versions(db, None)

        assert resolved == ["config/ws-1/cv-1.tar.gz"]

    async def test_the_state_index_is_only_expected_when_state_exists(self):
        """It is the break-glass recovery index. On an empty install its absence
        is correct, and reporting it missing would be noise."""
        empty = self._db([], count=0)
        total, resolved = await blob_readiness._resolve_state_index(empty, None)
        assert (total, resolved) == (0, [])

        populated = self._db([], count=7)
        total, resolved = await blob_readiness._resolve_state_index(populated, None)
        assert (total, resolved) == (1, ["state/index.yaml"])


class TestTheCheckIsSafeToRun:
    async def test_an_unavailable_store_is_a_readout_not_an_exception(self):
        """The caller is an operator deciding whether to fail over. An exception
        tells them less than 'I could not look'."""
        with patch(
            "terrapod.storage.get_storage", side_effect=RuntimeError("no storage configured")
        ):
            result = await blob_readiness.check()

        assert result.unavailable_reason is not None
        assert "no storage configured" in result.unavailable_reason
        assert result.classes == []

    async def test_a_broken_class_does_not_abort_the_rest(self):
        """One unresolvable class is reported as an error on that class; the
        others still get checked. A partial readout beats no readout."""
        store = FakeStore()

        async def boom(db, limit):
            raise RuntimeError("bad query")

        with (
            patch("terrapod.storage.get_storage", return_value=store),
            patch("terrapod.db.session.get_db_session") as session,
            patch.object(
                blob_readiness,
                "RESOLVERS",
                [
                    ("broken", blob_readiness.IRREPLACEABLE, boom),
                    (
                        "fine",
                        blob_readiness.IRREPLACEABLE,
                        AsyncMock(return_value=(1, ["state/a/b.tfstate"])),
                    ),
                ],
            ),
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            session.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await blob_readiness.check()

        by_name = {c.name: c for c in result.classes}
        assert by_name["broken"].error == "bad query"
        assert by_name["fine"].healthy is True

    async def test_sampling_is_the_default_and_bounded(self):
        """Full verification over an estate's state history is thousands of round
        trips, so it has to be asked for."""
        seen: list[int | None] = []

        async def resolver(db, limit):
            seen.append(limit)
            return 0, []

        with (
            patch("terrapod.storage.get_storage", return_value=FakeStore()),
            patch("terrapod.db.session.get_db_session") as session,
            patch.object(
                blob_readiness, "RESOLVERS", [("x", blob_readiness.IRREPLACEABLE, resolver)]
            ),
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            session.return_value.__aexit__ = AsyncMock(return_value=False)

            sampled = await blob_readiness.check()
            full = await blob_readiness.check(full=True)

        assert seen == [blob_readiness.DEFAULT_SAMPLE, None]
        assert sampled.sampled is True
        assert full.sampled is False

    async def test_a_sampled_class_is_never_marked_complete(self):
        """The structural half of the honesty property: `complete` is derived
        from whether a limit applied, not asserted by the resolver."""
        with (
            patch("terrapod.storage.get_storage", return_value=FakeStore()),
            patch("terrapod.db.session.get_db_session") as session,
            patch.object(
                blob_readiness,
                "RESOLVERS",
                [
                    (
                        "x",
                        blob_readiness.IRREPLACEABLE,
                        AsyncMock(return_value=(9999, ["state/a/b.tfstate"])),
                    )
                ],
            ),
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            session.return_value.__aexit__ = AsyncMock(return_value=False)

            sampled = await blob_readiness.check()
            full = await blob_readiness.check(full=True)

        assert sampled.classes[0].complete is False
        assert sampled.classes[0].total_rows == 9999
        assert full.classes[0].complete is True

    @pytest.mark.parametrize("sample", [0, -5])
    async def test_a_nonsense_sample_still_checks_something(self, sample):
        """A caller passing 0 should not silently get a check that verified
        nothing and reported healthy."""
        seen: list[int | None] = []

        async def resolver(db, limit):
            seen.append(limit)
            return 0, []

        with (
            patch("terrapod.storage.get_storage", return_value=FakeStore()),
            patch("terrapod.db.session.get_db_session") as session,
            patch.object(
                blob_readiness, "RESOLVERS", [("x", blob_readiness.IRREPLACEABLE, resolver)]
            ),
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            session.return_value.__aexit__ = AsyncMock(return_value=False)
            await blob_readiness.check(sample=sample)

        assert seen == [1]

    def test_no_db_session_is_held_across_the_presence_checks(self):
        """The presence checks are the slow part. Holding a pooled connection
        through them is how an observability feature starves the thing it
        observes — so the session is opened per class and closed before the
        round trips start."""
        import inspect

        source = inspect.getsource(blob_readiness.check)

        resolve_at = source.index("await resolver(")
        exit_at = source.index("await _check_keys(")
        assert resolve_at < exit_at
        assert "async with get_db_session() as db:" in source
